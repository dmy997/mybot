# 上下文管理系统 (Context Management)

## 概述

mybot 的上下文管理系统负责"LLM 看到什么"的完整生命周期：系统提示词组装、会话历史加载、token 预算检查、多级上下文压缩、记忆注入，以及会话中断修复。核心入口是 `ContextManager`。

## 架构总览

```
ContextManager
├── TokenBudget          — 所有阈值的唯一配置来源
├── SessionManager       — 会话 JSON 持久化 + 游标管理
├── MemoryStore          — 文件 I/O（MEMORY.md, history.jsonl, SOUL.md, USER.md）
├── Consolidator         — 实时 token 预算驱动的 LLM 摘要（fire-and-forget）
├── CompactionService    — 光标推进压缩（无 LLM，两层）
│   ├── Layer 1: micro_compact  (规则, 无 LLM)
│   └── Layer 2: auto_compact   (仅推进 consolidated_cursor，不调用 LLM)
└── SkillsLoader         — Skill 发现与注入
```

## 核心流程: build_messages()

`context/context_manager.py`

每次 Agent 运行前，`build_messages()` 组装完整的消息列表:

```python
async def build_messages(
    self,
    session_key: str,
    current_input: str,
    *,
    tools: ToolRegistry | None = None,
    skills: list[str] | None = None,
) -> list[dict[str, Any]]:
```

组装顺序：

1. **修复中断会话** — 调用 `_repair_messages()` 补全不完整的 tool_call/tool_result 对
2. **Micro-compact** — 清除旧轮次的工具结果（Layer 1，纯规则）
3. **过滤系统消息** — 移除历史中的 system 角色消息
4. **截断工具结果/参数** — 按 `TokenBudget` 配置限制长度
5. **构建系统提示词** — 调用 `_build_system_prompt()` 组装提示词
6. **多级 token 预算检查**：
   - `> block_threshold` → 强制压缩
   - `> auto_compact_threshold` → 自动压缩
   - `> warning_threshold` → 日志警告

## 系统提示词组装

采用 **两层缓存 + 动态注入** 结构：

```
Layer 1: Static（base prompt + skills + tools）
    └─ 缓存，仅在工具变更时失效

Layer 2: Memory context（SOUL.md + USER.md + MEMORY.md 长期记忆）
    └─ 按 (session, query_bucket) 缓存，remember/forget 时失效

Dynamic（永不缓存）:
    ├─ File context — 从最近消息中提取文件路径引用
    └─ Recent History — history.jsonl 中 Dream 尚未处理的条目
```

### Recent History 注入

`ContextManager._build_history_context()` — 这是 Consolidator 写入 `memory/history.jsonl` 后、Dream 将其合并到 `MEMORY.md` 前的过渡层：

```python
def _build_history_context(self, max_entries=20, max_chars=16_000) -> str:
    """Build a 'Recent History' section from unprocessed history.jsonl entries."""
    dream_cursor = self.store.get_dream_cursor()
    entries = self.store.read_history(since_cursor=dream_cursor)
    # 将 Dream 尚未处理的摘要条目注入提示词
```

这确保：Consolidator 刚写入的摘要无需等 2 小时 Dream 周期，在下一轮对话中就立即可见。

### 长期记忆注入

`MemoryStore.get_memory_context()` — 将 `MEMORY.md` 内容直接注入提示词：

```python
def get_memory_context(self) -> str:
    content = self.read_memory_file()
    if not content.strip() or self._is_template_content(content):
        return ""
    return f"## Long-term Memory\n\n{content}"
```

## Token 预算

`context/token_budget.py` — 所有阈值集中管理：

```python
@dataclass
class TokenBudget:
    context_window: int = 200_000          # 模型上下文窗口
    max_output_tokens: int = 20_000        # 预留给模型输出的空间

    # 四级阈值（计算属性）
    effective_window: int                  # context_window - max_output_tokens
    warning_threshold: int                 # effective_window - 20_000
    auto_compact_threshold: int            # effective_window - 13_000
    block_threshold: int                   # effective_window - 3_000

    # 各阶段截断限制
    tool_result_max_chars: int = 6_000     # 新工具结果
    history_tool_result_max_chars: int = 4_000  # 历史工具结果
    tool_call_args_max_chars: int = 10_000      # 工具调用参数

    # 压缩参数
    compress_ratio: float = 0.5            # 压缩时给最近消息保留的比例
    max_history_messages: int = 100        # 最多加载的历史消息数
    idle_compress_seconds: int = 300       # 空闲压缩触发秒数
```

## 会话级压缩: CompactionService

`context/compaction.py`

CompactionService 负责**会话内上下文压缩**，仅推进游标，不生成 LLM 摘要。LLM 摘要职责已转移至 Consolidator。

### Layer 1: micro_compact（规则，无 LLM 调用）

```python
@staticmethod
def micro_compact(messages, keep_recent_turns=2, placeholder="[Old tool result cleared]"):
    """清除超过 keep_recent_turns 轮的工具结果。"""
```

每次 `build_messages()` 前执行，返回新列表，不修改原始消息。

### Layer 2: auto_compact（光标推进，无 LLM）

```python
async def auto_compact(self, session_key, messages, *,
                       budget_tokens=None, keep_recent=None) -> CompactionResult:
```

两种触发方式：
- **Token 预算压缩**: 提供 `budget_tokens`，保留在该预算内的最近消息
- **空闲压缩**: 提供 `keep_recent`，保留最近 N 条消息

核心步骤：
1. 根据 `budget_tokens` 或 `keep_recent` 确定保留数量
2. 按角色边界调整分割点（`_adjust_split`），避免在 tool_call/tool_result 中间切断
3. 推进 `consolidated_cursor`——**不调用 LLM，不写入文件**

### full_compact（用户触发）

与 auto_compact 相同流程。`instructions` 参数保留用于 API 兼容但不实际使用（LLM 摘要由 Consolidator 处理）。

### 压缩的非破坏性设计

关键设计原则：**`session.messages` 永远不被修改**。压缩通过推进 `consolidated_cursor` 来实现"跳过"已压缩的消息：

```python
session.consolidated_cursor = len(session.messages) - len(to_keep)
```

## 记忆提取: Consolidator

`memory/consolidator.py`

Consolidator 负责**跨会话的事实提取和 LLM 摘要**（`memory/history.jsonl`）。

```python
class Consolidator:
    def __init__(self, store, provider=None, model="", *,
                 context_window_tokens=128_000, consolidation_ratio=0.7): ...

    async def maybe_consolidate(session, build_messages_fn=None) -> bool:
        """Token 预算检查 → LLM 摘要 → memory/history.jsonl"""
```

在 `Orchestrator.process_message()` 中 fire-and-forget 执行：

```python
asyncio.create_task(
    self.ctx.consolidator.maybe_consolidate(session, build_messages_fn=_build_fn)
)
```

**与 CompactionService 的区别**：

| 维度 | CompactionService | Consolidator |
|------|------------------|-------------|
| 写入位置 | (不写入文件 — 仅推进游标) | `memory/history.jsonl` |
| 目的 | 压缩会话上下文（跳过旧消息） | 提取长期记忆事实 |
| 触发 | Token 预算 / 空闲 | Token 预算（每次对话后检查） |
| 下游消费者 | 仅当前会话 | Dream → MEMORY.md |

## 会话交换保存

`context/context_manager.py`

`save_exchange()` 在每轮对话后持久化 user + assistant 消息：

```python
async def save_exchange(self, session_key, user_input, assistant_messages, *,
                        tools_used=None, errors=None):
    async with self.session.lock_session(session_key):
        session.messages.append({"role": "user", "content": user_input})
        for msg in assistant_messages:
            session.messages.append(msg)
        self.session.save_session(session)
```

## 会话中断修复

`context/context_manager.py`

`_repair_messages()` 在加载会话历史时执行，检测并修复三种中断模式：

1. assistant 发出了 tool_call 但 agent 在工具执行前崩溃 → 补全 tool_result
2. 工具正在执行时中断 → 补全结果
3. 收到 user 输入但 agent 还未响应 → 追加中断提示

## 设计要点

- **双系统并存**: CompactionService（游标推进，无 LLM）+ Consolidator（LLM 摘要 → history.jsonl），职责清晰
- **非破坏性压缩**: 原始消息永不修改，通过游标推进实现渐进压缩
- **分区缓存**: 提示词静态层 + 记忆层独立缓存，不同频率的失效互不干扰
- **记忆可见性**: Consolidator 写入的摘要立即可见（Recent History），Dream 合并后转为长期记忆
- **废止**: SessionMemory 已移除（587 行），Path B 质量评分逻辑同步删除；breaker 逻辑已移除（CompactionService 不再调用 LLM）
