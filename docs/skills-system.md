# Skills 系统 (Skills System)

## 概述

mybot 的 Skills 系统通过文件目录树管理 Agent 能力扩展。每个 Skill 是一个包含 `SKILL.md` 文件的目录，其中 YAML frontmatter 声明元数据（名称、描述、依赖项），正文是教 Agent 如何使用特定工具和完成特定任务的 Markdown 指令。系统支持渐进加载——系统提示词中只注入技能摘要，Agent 可通过 `read_file` 按需加载完整内容。

## 目录结构

```
skills/                         # 内置 Skill（随 mybot 分发）
├── docx/SKILL.md
├── pptx/SKILL.md
├── frontend-design/SKILL.md
├── canvas-design/SKILL.md
└── ...（共 14 个 Skill）

workspace/skills/               # 用户自定义 Skill（覆盖内置）
└── my-custom-skill/SKILL.md
```

用户 Skill 优先级高于内置：workspace 中的同名 Skill 会覆盖内置版本。

## SkillsLoader

`core/skills.py:21-242`

```python
class SkillsLoader:
    def __init__(self, workspace, builtin_skills_dir=None, disabled_skills=None):
        self.workspace = workspace
        self.workspace_skills = workspace / "skills"         # 用户 skill 目录
        self.builtin_skills = builtin_skills_dir or BUILTIN_SKILLS_DIR  # 内置
        self.disabled_skills = disabled_skills or set()
```

### Skill 文件格式

每个 Skill 目录下必须包含 `SKILL.md`，格式为 YAML frontmatter + Markdown 正文：

```markdown
---
name: frontend-design
description: 创建高质量前端界面
metadata:
  mybot:
    always: true        # 标记为 always skill，自动注入上下文
requires:
  bins: []              # 依赖的 CLI 命令
  env: []               # 依赖的环境变量
---

# Frontend Design Skill

## 使用指南
...
```

### 元数据解析

`core/skills.py:170-187`

```python
def _parse_skill_metadata(self, raw: object) -> dict:
    """解析 metadata 字段，兼容 dict 和 JSON 字符串两种格式。

    查找顺序：metadata.mybot → metadata.nanobot → metadata.openclaw
    """
    if isinstance(raw, dict):
        data = raw
    elif isinstance(raw, str):
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
    payload = data.get("mybot", data.get("nanobot", data.get("openclaw", {})))
    return payload if isinstance(payload, dict) else {}
```

### 核心方法一览

| 方法 | 返回值 | 用途 |
|------|--------|------|
| `list_skills(filter_unavailable)` | `list[dict]` | 列出所有 skill（名称、路径、来源） |
| `load_skill(name)` | `str \| None` | 加载单个 skill 的完整 Markdown 内容 |
| `load_skills_for_context(names)` | `str` | 加载多个 skill 正文（去 frontmatter，`---` 分隔） |
| `build_skills_summary(exclude)` | `str` | 构建所有 skill 摘要列表（名称+描述+路径+可用性） |
| `get_skill_metadata(name)` | `dict \| None` | 获取 skill 的完整 frontmatter 元数据 |
| `get_always_skills()` | `list[str]` | 获取标记 `always: true` 且依赖满足的 skill 名称 |
| `_check_requirements(meta)` | `bool` | 检查依赖项（CLI 命令 + 环境变量）是否满足 |

## Skills 组装流程（完整调用链）

Skills 的组装发生在**系统提示词构建阶段**，由 `ContextManager` 统一执行。Skills **不作为** `AgentInput` 的独立字段传递，而是在 `build_messages()` 时直接烘焙进系统提示词。

### 调用链总览：从入口到 Skill 注入

```
═══════════════════════════════════════════════════════════════════════════
入口层
═══════════════════════════════════════════════════════════════════════════

CLI (orchestrator.py:776-878)
  └── orche.process_message(session_key, user_input)
        # skills 参数未传入 → 默认为 None
        # ⚠️ 当前未调用 get_always_skills()

HTTP/WS (orchestrator.py:550)
  └── orche.process_message(..., skills=msg.skills)
        # 从 InboundMessage 提取用户指定的 skill 列表

═══════════════════════════════════════════════════════════════════════════
Orchestrator.process_message() — orchestrator.py:254-409
═══════════════════════════════════════════════════════════════════════════

line 274:  active_skills = list(skills or [])
           # ⚠️ get_always_skills() 未接入，待实现

line 279:  messages = await self.ctx.build_messages(
               session_key, user_input,
               tools=self._tools,
               skills=active_skills or None,   ← 传入 skills
           )

line 311:  spec = AgentInput(init_messages=messages, ...)
           # AgentInput 没有 skills 字段——skills 已烘焙进 system prompt

═══════════════════════════════════════════════════════════════════════════
ContextManager.build_messages() — context_manager.py:276-382
═══════════════════════════════════════════════════════════════════════════

line 326:  system_content = await self._build_system_prompt(
               session_key, tools=tools, skills=skills, query=current_input,
               messages=history,
           )

line 332:  preliminary = [
               {"role": "system", "content": system_content},  ← skills 在此
           ] + history + [{"role": "user", "content": current_input}]

═══════════════════════════════════════════════════════════════════════════
ContextManager._build_system_prompt() — context_manager.py:498-555
═══════════════════════════════════════════════════════════════════════════

三层缓存分区：

Layer 1 (静态, line 522):  static = await _build_static_prompt(tools, skills)
                           # 首次构建后无限期缓存
                           # 仅在 _invalidate_static() 时重建（tools 变更触发）

Layer 2 (记忆, line 531):  memory_ctx = _build_memory_context()
                           # 按 (session, query_bucket) 缓存
                           # 在 remember()/forget() 时失效

Dynamic (每次重建, line 544+):
  - file_ctx  = _extract_file_context(messages)
  - history_ctx = _build_history_context()
```

### _build_static_prompt 内部详细流程

`context_manager.py:570-614`

```
_build_static_prompt(tools, skills)
  │
  ├── 1. 缓存检查
  │     if self._static_prompt is not None: return self._static_prompt
  │
  ├── 2. 基础系统提示词
  │     parts.append(self.system_prompt or _DEFAULT_SYSTEM_PROMPT)
  │
  ├── 3. Skills 两级组装
  │     │
  │     ├── autoload_skills = self.skills_loader.build_skills_summary()
  │     │   (core/skills.py:111-142)
  │     │   │
  │     │   ├── list_skills(filter_unavailable=False)  # 获取全部 skill
  │     │   ├── 遍历每个 skill:
  │     │   │   ├── _get_skill_meta(name)  → 解析 metadata 字段
  │     │   │   ├── _check_requirements(meta)  → 检查 bins + env
  │     │   │   ├── _get_skill_description(name)  → 从 frontmatter 取 description
  │     │   │   └── 可用: "- **name** — desc  `path`"
  │     │   │       不可用: "- **name** — desc (unavailable: CLI: xxx)  `path`"
  │     │   └── 返回 "\n".join(lines)
  │     │
  │     ├── explicit_skills = self.skills_loader.load_skills_for_context(skills or [])
  │     │   (core/skills.py:94-109)
  │     │   │
  │     │   ├── for name in skill_names:
  │     │   │   ├── load_skill(name)
  │     │   │   │   ├── 先查 workspace/skills/{name}/SKILL.md
  │     │   │   │   └── 再查 builtin skills/{name}/SKILL.md
  │     │   │   ├── _strip_frontmatter(content)  # 去除 YAML frontmatter
  │     │   │   └── "### Skill: {name}\n\n{body}"
  │     │   └── 返回 "\n\n---\n\n".join(parts)
  │     │
  │     ├── skills_content = "\n\n".join([autoload_skills, explicit_skills])
  │     └── parts.append(
  │           render_template("agent/skills_section.md",
  │                           skills_summary=skills_content)
  │         )
  │         # 模板 prompt_templates/agent/skills_section.md:
  │         #   # Available Skills
  │         #   {{ skills_summary }}
  │
  ├── 4. 工具列表
  │     parts.append("# Available Tools\n\n- **tool_name**: desc ...")
  │
  ├── 5. 缓存并返回
  │     self._static_prompt = "\n\n".join(parts)
  │     return self._static_prompt
```

### 最终系统提示词结构

```
<base system prompt ("Reply in the same language...")>

# Available Skills

- **docx** — 创建/编辑 Word 文档  `skills/docx/SKILL.md`
- **xlsx** — 创建/编辑 Excel 文件  `skills/xlsx/SKILL.md`
- **frontend-design** — 创建高质量前端界面  `skills/frontend-design/SKILL.md`
... (14 个 skill 的摘要列表)

### Skill: docx

<docx SKILL.md 完整正文，去除了 YAML frontmatter>

---

### Skill: xlsx

<xlsx SKILL.md 完整正文>

---

# Identity (SOUL.md)
...

---

# User Profile (USER.md)
...

---

# Available Tools

- **bash**: 执行 shell 命令
...
```

## Skill 发现与加载的完整文件 I/O 路径

```
load_skill("xlsx")
  ├── workspace/skills/xlsx/SKILL.md  → 优先读取
  └── skills/xlsx/SKILL.md            → 回退读取（内置）

list_skills()
  ├── _skill_entries_from_dir(workspace/skills, "workspace")
  │     └── 遍历 workspace/skills/*/SKILL.md
  ├── _skill_entries_from_dir(skills, "builtin", skip_names=workspace_names)
  │     └── 遍历 skills/*/SKILL.md，跳过已由 workspace 覆盖的名称
  ├── 过滤 disabled_skills
  └── (可选) 过滤 _check_requirements() 失败的 skill
```

## 渐进加载机制

两级粒度：

1. **摘要级**（`build_skills_summary()`）：始终注入系统提示词。包含所有 skill 的名称、一句话描述、文件路径。Agent 看到这些摘要后，可以用 `read_file` 工具按需加载完整内容。

2. **完整级**（`load_skills_for_context()`）：仅对用户显式请求的 skill（通过 `skills` 参数传入）注入完整 Markdown 正文。

这种设计确保：
- 系统提示词体积可控（15 个 skill 摘要 ≈ 2KB，而 15 个完整 skill ≈ 50KB+）
- Agent 知道有哪些能力可用（通过摘要）
- 需要时按需加载完整指令（通过 `read_file` 工具）

## Always Skills

`core/skills.py:203-213`

```python
def get_always_skills(self) -> list[str]:
    """获取标记为 always=true 且依赖满足的 skill 名称列表。"""
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

在前端设计中，`metadata.mybot.always: true` 标记的 skill（如 `frontend-design`）应该自动加载到上下文中，无需用户显式调用。

**当前状态**：`get_always_skills()` 方法已实现，但 **尚未接入 Orchestrator**。`Orchestrator.process_message()` 中 skill 组装逻辑仅为：

```python
# orchestrator.py:274
active_skills = list(skills or [])  # 仅使用传入的 skills，未调用 get_always_skills()
```

接入方式：在此行合并 `get_always_skills()` 的结果：

```python
active_skills = list(skills or [])
active_skills.extend(self.ctx.skills_loader.get_always_skills())
```

## 依赖项检查

`core/skills.py:189-196`

```python
def _check_requirements(self, skill_meta: dict) -> bool:
    requires = skill_meta.get("requires", {})
    required_bins = requires.get("bins", [])
    required_env_vars = requires.get("env", [])
    return all(shutil.which(cmd) for cmd in required_bins) and all(
        os.environ.get(var) for var in required_env_vars
    )
```

依赖项在多个环节影响行为：
- `list_skills(filter_unavailable=True)`：过滤掉依赖不满足的 skill
- `build_skills_summary()`：不可用 skill 显示为 `(unavailable: CLI: xxx)`
- `get_always_skills()`：仅返回依赖满足的 always skill

## 发现优先级

`core/skills.py:51-73`

```
list_skills()
  ├── 1. 扫描 workspace/skills/  —— 用户自定义 skill（高优先级）
  ├── 2. 扫描 builtin skills/     —— 内置 skill，跳过 workspace 中已存在的同名
  ├── 3. 过滤 disabled_skills
  └── 4. 过滤依赖不满足的 skill（当 filter_unavailable=True）
```

## 缓存策略

Skills 内容属于**静态提示词层**（Layer 1），在 `ContextManager` 中无限期缓存：

- **首次构建**：`_build_static_prompt()` 生成并存入 `self._static_prompt`
- **后续请求**：直接返回缓存，不重新扫描 skill 目录
- **失效触发**：仅当调用 `_invalidate_static()` 时（tools 注册/注销时触发）

这意味着**运行时新增或修改 skill 文件不会自动生效**，需要重启或触发 tools 变更。

## 设计要点

- **渐进加载**：系统提示词中只有摘要（名称+描述+路径），Agent 按需用 `read_file` 加载完整 Skill
- **用户覆盖**：`workspace/skills/` 中的 Skill 优先于内置
- **依赖感知**：缺少 CLI 工具或环境变量的 Skill 自动标记为不可用
- **兼容性**：支持 `metadata.mybot`（以及旧版 `metadata.nanobot`/`metadata.openclaw`），支持 dict 和 JSON 字符串
- **always 机制**：方法已就绪，待接入 Orchestrator 的 `process_message()`
- **静态缓存**：Skill 内容无限期缓存，运行时新增/修改 skill 需重启生效
- **Skill 非 Agent 概念**：Skill 在系统提示词构建阶段烘焙，Agent 和 AgentInput 对 Skill 无感知
