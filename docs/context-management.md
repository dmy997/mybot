# 上下文管理系统 (Context Management)

## 概述

mybot 的上下文管理系统负责"LLM 看到什么"的完整生命周期：系统提示词组装、会话历史加载、token 预算检查、多级上下文压缩，以及会话中断修复。核心入口是 `ContextManager`，它协调 `TokenBudget`（阈值配置）、`CompactionService`（三层压缩金字塔）、`MemoryService`（记忆检索）和 `SessionManager`（持久化）协同工作。

## 架构总览

```
ContextManager
├── TokenBudget          — 所有阈值的唯一配置来源
├── SessionManager       — 会话 JSON 持久化 + 游标管理
├── MemoryManager        — 长期记忆 CRUD
├── MemoryService        — LLM 辅助记忆相关性过滤
├── CompactionService    — 三层压缩金字塔
│   ├── Layer 1: micro_compact  (规则, 无 LLM)
│   ├── Layer 2: auto_compact   (LLM 摘要, 持久化)
│   └── Layer 3: full_compact   (用户触发)
└── SkillsLoader         — Skill 发现与注入
```

## 核心流程: build_messages()

`context/context_manager.py:306-417`

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
5. **构建系统提示词** — 调用 `_build_system_prompt()` 组装四层提示词
6. **多级 token 预算检查**：
   - `> block_threshold` → 强制压缩
   - `> auto_compact_threshold` → 自动压缩（含断路器）
   - `> warning_threshold` → 日志警告

```python
# 多级阈值检查 (context/context_manager.py:369-416)
total = _estimate_message_tokens(preliminary)

if total > budget.block_threshold:
    # 强制压缩 — 不压缩不允许继续
    result = await self.compaction.auto_compact(...)
elif total > budget.auto_compact_threshold:
    # 自动压缩 — 有断路器保护
    if self.compaction.can_auto_compact():
        result = await self.compaction.auto_compact(...)
elif total > budget.warning_threshold:
    logger.warning("Context at {} tokens ...", total)
```

## 系统提示词组装

`context/context_manager.py:546-628`

采用**三层分区缓存**策略：

```python
async def _build_system_prompt(self, session_key, tools, skills, query, messages) -> str:
    parts: list[str] = []

    # Layer 1: Static（base + skills + tools）— 缓存，仅在工具变更时失效
    static = await self._build_static_prompt(tools, skills)

    # Layer 2: Memory context（SOUL + USER + MEMORY + 相关条目）
    # 按 (session, query_bucket) 缓存，remember/forget 时失效
    mem_key = f"{session_key or 'default'}:{hash(query or '') % 20}"
    if mem_key not in self._memory_cache:
        self._memory_cache[mem_key] = await self.memory_service.build_memory_context(...)

    # Layer 3: History summaries（压缩归档）— 按 session 缓存
    if session_key not in self._history_cache:
        self._history_cache[session_key] = self.compaction.read_history_summaries(...)

    # Layer 4: Dynamic（session notes + file context）— 永不缓存
    notes_ctx = notes.get_compact_summary()
    file_ctx = self._extract_file_context(messages)

    return "\n\n".join(parts)
```

**缓存失效策略**：
- `_invalidate_static()` — 注册/注销工具时
- `_invalidate_memory_cache()` — remember/forget 时
- `_invalidate_history_cache()` — 压缩写入新摘要后

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
    max_consecutive_failures: int = 3      # 断路器最大连续失败次数
```

## 三层压缩金字塔

`context/compaction.py`

### Layer 1: micro_compact（规则，无 LLM 调用）

```python
@staticmethod
def micro_compact(messages, keep_recent_turns=2, placeholder="[Old tool result cleared]"):
    """清除超过 keep_recent_turns 轮的工具结果。"""
    total_turns = sum(1 for m in messages
                      if m.get("role") == "assistant" and m.get("tool_calls"))
    cutoff = max(0, total_turns - keep_recent_turns)
    # 将旧轮次的 tool 消息替换为 placeholder
```

每次 `build_messages()` 前执行，返回新列表，不修改原始消息。

### Layer 2: auto_compact（LLM 摘要，持久化到 history.jsonl）

```python
async def auto_compact(self, session_key, messages, *,
                       budget_tokens=None, keep_recent=None,
                       session_memory=None) -> CompactionResult:
```

两种触发方式：
- **Token 预算压缩**: 提供 `budget_tokens`，保留在该预算内的最近消息
- **空闲压缩**: 提供 `keep_recent`，保留最近 N 条消息

核心步骤：
1. 根据 `budget_tokens` 或 `keep_recent` 确定保留数量
2. 按角色边界调整分割点（`_adjust_split`），避免在 tool_call/tool_result 中间切断
3. **脱水** (`dehydrate_messages`) — 截断长内容、移除 base64 data URI
4. **LLM 摘要** — 调用小模型生成摘要（约 200 词）
5. 写入 `history.jsonl`，推进 `consolidated_cursor`

**Path B 快捷路径**: 当 `SessionMemory` 质量评分 >= 50 时，直接用结构化笔记作为摘要，节省一次 LLM 调用：

```python
# context/compaction.py:493-512
if session_memory.is_fresh() and session_memory.has_substance():
    score = session_memory.quality_score()
    if score >= 50:
        notes_summary = session_memory.get_compact_summary()
        if notes_summary.strip():
            return notes_summary  # 跳过 LLM 摘要
```

**断路器**: 连续失败 3 次后，`can_auto_compact()` 返回 False，阻止进一步压缩尝试。

### Layer 3: full_compact（用户触发）

```python
async def full_compact(self, session_key, messages, *,
                       instructions=None, budget_tokens=None,
                       session_memory=None) -> CompactionResult:
```

与 auto_compact 相同流程，但绕过断路器且支持自定义摘要指令。

### 脱水（Dehydration）

发送给摘要 LLM 前，剥离非关键负载：

```python
def dehydrate_messages(messages, max_content_chars=3000):
    # - 截断 content 字符串到 max_content_chars
    # - 替换 base64 data URI 为 "[binary data removed]"
    # - 移除工具调用参数，仅保留函数名
```

### 历史摘要读取

压缩后的摘要存入 `{session_key}_history.jsonl`，在系统提示词组装时注入：

```python
def read_history_summaries(self, session_key, max_entries=10, max_chars_per_entry=2000):
    # 读取最后 max_entries 条，每条截断至 max_chars_per_entry
    # 返回 "# Previous Conversation Summaries\n\n## Historical Summary (...)" 格式
```

## 压缩的非破坏性设计

关键设计原则：**`session.messages` 永远不被修改**。压缩通过推进 `consolidated_cursor` 来实现"跳过"已压缩的消息，而不是从数组中删除它们：

```python
# auto_compact 中推进游标
session.consolidated_cursor = len(session.messages) - len(to_keep)
```

这保证了已压缩的消息仍可用于调试、恢复和未来重新摘要。

## 会话中断修复

`context/context_manager.py:731-774`

`_repair_messages()` 在加载会话历史时执行，检测并修复三种中断模式：

```python
@staticmethod
def _repair_messages(messages) -> tuple[list, int]:
    # Pass 1: 为没有对应 tool_result 的 tool_call 补全占位结果
    for msg in messages:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                if tc_id not in tool_results:
                    repaired.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": "Error: Tool execution interrupted.",
                    })

    # Pass 2: 如果最后一条消息是 user 或 tool，追加中断提示
    if repaired[-1].get("role") in ("user", "tool"):
        repaired.append({
            "role": "assistant",
            "content": "Error: Task interrupted before a response was generated.",
        })
```

三种中断场景：
1. assistant 发出了 tool_call 但 agent 在工具执行前崩溃 → 补全 tool_result
2. 工具正在执行时中断 → Pass 1 补全结果
3. 收到 user 输入但 agent 还未响应 → Pass 2 追加中断提示

## 文件上下文恢复

`context/context_manager.py:789-827`

压缩后，旧消息中的文件引用会丢失。`_extract_file_context()` 从最近的消息中扫描文件路径，恢复模型对当前操作文件的感知：

```python
_FILE_PATH_RE = re.compile(
    r'(?:^|[\s`"\'(])'
    r'(/?[\w.-]+(?:/[\w.-]+)+\.\w{1,10}'
    r'|~?/\w+(?:/[\w.-]+)+\.?\w*)'
    r'|@[\w./-]+\.\w+',
)

@classmethod
def _extract_file_context(cls, messages, max_files=5):
    # 从最新到最旧扫描，找到最近引用的文件路径（最多 5 个）
    # 返回 "# Files in Context\n\n- `/path/to/file`" 格式
```

## 会话交换保存

`context/context_manager.py:450-504`

`save_exchange()` 在每轮对话后持久化 user + assistant 消息，同时更新结构化会话笔记：

```python
async def save_exchange(self, session_key, user_input, assistant_messages, *,
                        tools_used=None, errors=None):
    async with self.session.lock_session(session_key):
        session.messages.append({"role": "user", "content": user_input})
        for msg in assistant_messages:
            session.messages.append(msg)
        self.session.save_session(session)

    # 更新结构化笔记（同步规则 + 异步 fork-agent）
    notes.update(user_input=user_input, assistant_content=..., tools_used=tools_used)
    # fork-agent 在后台使用 LLM 提炼关键决策、文件、错误
    asyncio.create_task(notes.update_async(self.provider, ...))
```

## AgentCore 中的轻量级压缩

`core/runner.py:562-660`

当 `CompactionService` 未注入时（如直接使用 `AgentCore`），回退到内置的轻量级 3 步压缩：

```python
@staticmethod
def _lightweight_compact(messages, max_tokens=128_000, trigger_ratio=0.8):
    # Step 1: 摘要旧轮次的工具结果（替换为 "[Compacted] ..."）
    # Step 2: 移除孤立的 tool_result（无匹配 tool_call）
    # Step 3: 补全缺失的 tool_result（tool_call 无结果）
    # 返回新列表，原始 messages 不变
```

## 设计要点

- **单一配置源**: `TokenBudget` 集中管理所有阈值，避免硬编码分散
- **非破坏性压缩**: 原始消息永不修改，通过游标推进和摘要注入实现渐进压缩
- **分区缓存**: 提示词三层独立缓存，不同频率的失效互不干扰
- **Path B 快捷路径**: 结构化会话笔记质量足够时，跳过 LLM 摘要调用，节省延迟和成本
- **断路器保护**: 连续摘要失败后自动停止，防止在异常状态下反复消耗资源
