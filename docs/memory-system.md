# 记忆系统 (Memory System)

## 概述

mybot 的记忆系统是一个基于文件的长期记忆模块，采用 **store–manager–service** 三层架构。设计灵感来自 nanobot 的 MemoryStore，适配 Claude Code 兼容的记忆文件约定。

## 目录布局

```
workspace/
├── SOUL.md                    # AI 助手的自我描述
├── USER.md                    # 用户画像
└── memory/
    ├── MEMORY.md              # 所有记忆的索引文件
    ├── user/                  # 用户相关记忆
    │   └── <name>.md
    ├── feedback/              # 用户反馈记忆
    │   └── <name>.md
    ├── project/               # 项目相关记忆
    │   └── <name>.md
    └── reference/             # 参考资料记忆
        └── <name>.md
```

## 三层架构

### 1. MemoryStore — 纯文件 I/O

`memory/store.py:22-210`

MemoryStore 是系统的存储底层，只做文件读写，不涉及业务逻辑。

```python
class MemoryStore:
    """Pure file I/O for memory files: MEMORY.md, individual .md files."""

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).expanduser().resolve()
        self.memory_dir = self.workspace / "memory"
        self._index_file = self.memory_dir / _MEMORY_INDEX
        self._soul_file = self.workspace / _SOUL_FILE
        self._user_file = self.workspace / _USER_FILE
        self._ensure_dirs()
```

**核心文件方法** (`memory/store.py:108-131`)：

```python
def read_memory(self, name: str) -> MemoryEntry | None:
    """Read a memory entry by name. Scans all type dirs."""
    for mtype in MEMORY_TYPES:
        file_path = self.memory_dir / mtype / f"{name}.md"
        if file_path.exists():
            text = self._read_file(file_path)
            entry = MemoryEntry.from_frontmatter_text(text)
            if entry:
                entry.file_path = str(file_path.relative_to(self.memory_dir))
                return entry
    return None

def write_memory(self, entry: MemoryEntry) -> Path:
    """Write a memory entry to its type subdirectory and update MEMORY.md."""
    file_path = self.memory_dir / entry.relative_path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    self._write_file(file_path, entry.to_frontmatter_text())
    self._upsert_index(entry)
    return file_path
```

**索引管理** (`memory/store.py:186-209`)：

```python
def _upsert_index(self, entry: MemoryEntry) -> None:
    """Add or update an entry in MEMORY.md."""
    lines = self.read_memory_index().splitlines()
    new_line = f"- [{entry.name}]({entry.relative_path}) — {entry.description}"
    replaced = False
    for i, line in enumerate(lines):
        parsed = parse_memory_index_line(line)
        if parsed and parsed[0] == entry.name:
            lines[i] = new_line
            replaced = True
            break
    if not replaced:
        lines.append(new_line)
    self.write_memory_index("\n".join(lines) + "\n")
```

**反向同步** (`memory/store.py:163-182`) — 检测被外部工具修改的记忆文件：

```python
def check_reverse_sync(self) -> list[str]:
    """Check for markdown files modified externally (newer than index)."""
    # 比较每个记忆文件的 mtime 与索引文件的 mtime
    # 返回需要重新索引的记忆名称列表
```

### 2. MemoryManager — 高级 CRUD + 关键词检索

`memory/manager.py:28-141`

```python
class MemoryManager:
    """High-level memory API for the agent."""

    def remember(self, name: str, content: str, *,
                 mem_type: str = "user", description: str = "",
                 force: bool = False) -> MemoryEntry:
        """Create or update a memory entry."""
        if mem_type not in MEMORY_TYPES:
            raise ValueError(f"Invalid memory type: {mem_type}")

        existing = self.store.read_memory(name)
        if existing and not force:
            logger.info("Memory '{}' already exists, updating.", name)

        entry = MemoryEntry(
            name=name, type=mem_type,
            description=description, content=content,
        )
        self.store.write_memory(entry)
        return entry
```

**关键词检索** (`memory/manager.py:97-118`) — 当前 V1 实现采用纯关键词评分匹配：

```python
def recall(self, query: str, *, top_n: int = 10) -> list[MemoryEntry]:
    """Simple keyword-based memory retrieval."""
    query_lower = query.lower()
    scored: list[tuple[int, MemoryEntry]] = []

    for entry in self.store.list_memories():
        score = 0
        if query_lower in entry.name.lower():
            score += 10           # 名称匹配权重最高
        if query_lower in entry.description.lower():
            score += 5            # 描述匹配权重次之
        content_lower = entry.content.lower()
        score += content_lower.count(query_lower) * 2  # 内容按出现次数计分
        if score > 0:
            scored.append((score, entry))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [entry for _, entry in scored[:top_n]]
```

评分规则：名称匹配 10 分、描述匹配 5 分、内容每出现一次 2 分。后续可升级为 embedding 向量检索。

### 3. MemoryService — LLM 辅助过滤

`context/memory_service.py` — 位于 context 模块而非 memory 模块，因为需要 LLM 调用能力。它对 MemoryManager 的检索结果做二次过滤，通过 LLM 判断记忆与当前查询的相关性，并执行大小上限裁剪。

## 数据类型

`memory/types.py` — 定义了四种记忆类型和 MemoryEntry 数据结构：

```python
MEMORY_TYPES = ["user", "feedback", "project", "reference"]

@dataclass
class MemoryEntry:
    name: str           # kebab-case 唯一标识
    type: str           # user / feedback / project / reference
    description: str    # 一行摘要，用于索引文件
    content: str        # Markdown 正文
    file_path: str = "" # 相对于 memory_dir 的路径
```

文件格式采用 YAML frontmatter + Markdown 正文：

```markdown
---
name: my-memory
description: 一段描述
metadata:
  type: user
---

正文内容。
```

## 与上下文系统的集成

记忆通过 `ContextManager.build_messages()` 注入到 system prompt 中：

```python
# context/context_manager.py — 组装 system prompt 时注入记忆上下文
memory_section = self._build_memory_section()
# memory_section 包含 SOUL.md、USER.md、以及长期记忆的检索结果
```

SOUL.md（AI 助手画像）和 USER.md（用户画像）是特殊的记忆文件，直接放置在 workspace 根目录，由 `MemoryStore.read_soul()` / `read_user()` 读取。

## 设计要点

- **文件即数据源**：记忆以 Markdown 文件存储，可直接用编辑器修改，修改后通过 `check_reverse_sync()` 检测并重建索引
- **索引加速**：`MEMORY.md` 维护所有记忆的一行摘要，避免每次检索都扫描全目录
- **类型隔离**：四种类型分目录存储，语义清晰，互不干扰
