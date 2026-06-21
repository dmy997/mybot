# Skills 系统 (Skills System)

## 概述

mybot 的 Skills 系统通过文件目录树管理 Agent 能力扩展。每个 Skill 是一个包含 `SKILL.md` 文件的目录，其中 YAML frontmatter 声明元数据（名称、描述、依赖项），正文是教 Agent 如何使用特定工具和完成特定任务的 Markdown 指令。系统支持渐进加载 — 系统提示词中只注入技能摘要，Agent 可通过 `read_file` 按需加载完整内容。

## 目录结构

```
skills/                         # 内置 Skill（随 mybot 分发）
├── docx/SKILL.md
├── pptx/SKILL.md
├── frontend-design/SKILL.md
├── canvas-design/SKILL.md
└── ...

workspace/skills/               # 用户自定义 Skill（覆盖内置）
└── my-custom-skill/SKILL.md
```

用户 Skill 优先级高于内置：workspace 中的同名 Skill 会覆盖内置版本。

## SkillsLoader

`core/skills.py`

```python
class SkillsLoader:
    def __init__(self, workspace: Path, builtin_skills_dir=None, disabled_skills=None):
        self.workspace = workspace
        self.workspace_skills = workspace / "skills"
        self.builtin_skills = builtin_skills_dir or BUILTIN_SKILLS_DIR
        self.disabled_skills = disabled_skills or set()
```

### Skill 文件格式

```markdown
---
name: frontend-design
description: 创建高质量前端界面
metadata:
  mybot:
    always: true
requires:
  bins: []
  env: []
---

# Frontend Design Skill

## 使用指南
...
```

YAML frontmatter 字段:
- **name**: Skill 标识（目录名）
- **description**: 简短描述
- **metadata**: 嵌套元数据（支持 YAML 字典或 JSON 字符串格式）
- **requires.bins**: 需要系统中有哪些 CLI 命令
- **requires.env**: 需要设置哪些环境变量

### 元数据解析

```python
def _parse_skill_metadata(self, raw: object) -> dict:
    """提取 skill 元数据。
    支持 dict（已由 yaml.safe_load 解析）和 JSON 字符串两种格式。
    """
    if isinstance(raw, dict):
        data = raw
    elif isinstance(raw, str):
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
    else:
        return {}
    payload = data.get("mybot", data.get("nanobot", data.get("openclaw", {})))
    return payload if isinstance(payload, dict) else {}
```

### 发现与列表

```python
def list_skills(self, filter_unavailable=True) -> list[dict]:
    # 1. 扫描 workspace/skills/
    # 2. 扫描 builtin skills/，跳过 workspace 中已存在的同名 Skill
    # 3. 过滤掉 disabled_skills 中的 Skill
    # 4. 按依赖项可用性过滤
```

### 渐进加载

两个粒度级别：

**摘要级别**（注入系统提示词）：

```python
def build_skills_summary(self, exclude=None) -> str:
    """列出所有 Skill 的名称、描述、路径和可用性。
    格式: "- **skill-name** — 描述  `path/to/SKILL.md`"
    不可用的 Skill 会标注: "- **skill-name** — 描述 (unavailable: CLI: cmd)"
    """
```

**完整加载**（Agent 用 read_file 工具按需加载）：

```python
def load_skill(self, name: str) -> str | None:
    """按名称加载 Skill 的完整 Markdown 内容。"""

def load_skills_for_context(self, skill_names: list[str]) -> str:
    """加载指定 Skill，去除 frontmatter，用 --- 分隔。"""
    parts = [
        f"### Skill: {name}\n\n{self._strip_frontmatter(markdown)}"
        for name in skill_names
        if (markdown := self.load_skill(name))
    ]
    return "\n\n---\n\n".join(parts)
```

### 自动加载（Always Skills）

```python
def get_always_skills(self) -> list[str]:
    """获取标记为 always=true 且依赖项满足的 Skill 名称列表。"""
    return [
        entry["name"]
        for entry in self.list_skills(filter_unavailable=True)
        if (meta := self.get_skill_metadata(entry["name"]) or {})
        and (
            self._parse_skill_metadata(meta.get("metadata")).get("always")
            or meta.get("always")
        )
    ]
```

Always skills 自动注入系统提示词，无需 Agent 显式请求。

### 依赖项检查

```python
def _check_requirements(self, skill_meta: dict) -> bool:
    requires = skill_meta.get("requires", {})
    required_bins = requires.get("bins", [])
    required_env_vars = requires.get("env", [])
    return all(shutil.which(cmd) for cmd in required_bins) and all(
        os.environ.get(var) for var in required_env_vars
    )

def _get_missing_requirements(self, skill_meta: dict) -> str:
    """返回可读的缺失依赖描述。"""
    return ", ".join(
        [f"CLI: {name}" for name in required_bins if not shutil.which(name)]
        + [f"ENV: {name}" for name in required_env_vars if not os.environ.get(name)]
    )
```

## 与上下文的集成

`ContextManager._build_static_prompt()` 在系统提示词组装时注入 Skills：

```python
async def _build_static_prompt(self, tools, skills):
    # 1. 自动加载的 Skill 摘要
    autoload_skills = self.skills_loader.build_skills_summary()

    # 2. 显式请求的 Skill 完整内容
    explicit_skills = self.skills_loader.load_skills_for_context(skills or [])

    # 3. 通过 Jinja2 模板渲染合并
    skills_content = "\n\n".join(s for s in (autoload_skills, explicit_skills) if s)
    if skills_content:
        parts.append(render_template("agent/skills_section.md", skills_summary=skills_content))
```

## 禁用 Skill

通过 `disabled_skills` 参数传入，在发现和列表阶段即被排除：

```python
# ContextManager 构造时传入
ContextManager(workspace=..., disabled_skills=["slack-gif-creator"])
```

## 设计要点

- **渐进加载**: 系统提示词中只有摘要（名称+描述+路径），Agent 按需用 read_file 加载完整 Skill
- **用户覆盖**: workspace/skills/ 中的 Skill 优先于内置
- **依赖感知**: 缺少 CLI 工具或环境变量的 Skill 自动标记为不可用，不污染 Agent 选择
- **兼容性**: 支持 `metadata.mybot`（以及旧版 `metadata.nanobot`/`metadata.openclaw`），支持 dict 和 JSON 字符串
- **always 机制**: 标记为 always 的 Skill 自动注入，无需显式调用
