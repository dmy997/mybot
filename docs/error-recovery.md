# 错误恢复机制 (Error Recovery)

## 概述

mybot 在 LLM 调用层和 Agent 执行层都实现了分类错误处理和自动恢复机制。错误被分为三类（可重试/可恢复/致命），不同类别对应不同的处理策略。Agent 执行层还包含重试、上下文压缩恢复和断点续跑能力。

## 错误分类体系

`providers/errors.py`

### ErrorCategory 枚举

```python
class ErrorCategory(enum.Enum):
    RETRYABLE = "retryable"      # 瞬时错误 — 可带退避重试
    RECOVERABLE = "recoverable"  # 永久错误 — 可通过调整请求参数恢复
    FATAL = "fatal"              # 永久错误 — 无法恢复，立即中止
```

### LLMErrorInfo

```python
@dataclass
class LLMErrorInfo:
    category: ErrorCategory
    message: str
    status_code: int | None = None
    error_type: str | None = None    # 机器可读标签
    retry_after: float | None = None # Retry-After 头
    raw_error: BaseException | None = None
```

常见 `error_type` 标签：
- `"rate_limit"` (RETRYABLE) — 速率限制，等待后重试
- `"context_length"` (RECOVERABLE) — 上下文超长，压缩后重试
- `"content_filter"` (RECOVERABLE) — 内容过滤，追加合规提示后重试
- `"auth_error"` (FATAL) — 认证失败，直接中止
- `"bad_request"` (FATAL) — 请求格式错误，不重试
- `"server_error"` (RETRYABLE) — 服务端错误，退避重试

### 异常层级

```python
class RetryableLLMError(Exception):
    def __init__(self, info: LLMErrorInfo): ...

class RecoverableLLMError(Exception):
    def __init__(self, info: LLMErrorInfo): ...

class FatalLLMError(Exception):
    def __init__(self, info: LLMErrorInfo): ...
```

## Provider 层：错误分类

`providers/openai_compatible_provider.py`

Provider 的 `_classify_error()` 将 OpenAI API 异常映射为三类错误：

- HTTP 429 (Rate Limit) → `RetryableLLMError`，携带 `retry_after`
- HTTP 5xx 服务端错误 → `RetryableLLMError`
- HTTP 400 中 context_length_exceeded → `RecoverableLLMError`
- HTTP 400 中 content_filter → `RecoverableLLMError`
- HTTP 401/403 认证错误 → `FatalLLMError`
- 其他 HTTP 4xx → `FatalLLMError`

### 重试机制

`chat_with_retry()` 对 `RetryableLLMError` 使用指数退避重试：

```python
async def chat_with_retry(self, messages, tools, model, max_tokens, temperature):
    max_retries = 3
    for attempt in range(max_retries + 1):
        try:
            return await self.chat(messages, tools, model, max_tokens, temperature)
        except RetryableLLMError as e:
            if attempt == max_retries:
                raise  # 重试耗尽
            delay = e.info.retry_after or (2 ** attempt)
            await asyncio.sleep(delay)
```

## AgentCore 层：分类恢复

`core/runner.py:662-883`

`_call_llm()` 是 LLM 调用的核心方法，对三类错误采用不同的响应策略：

### 错误处理流程

```python
async def _call_llm(self, spec, messages, tool_defs, *, recovery_attempt=False, step_count=0):
    with tracer.span("llm.chat", model=model, ...):
        try:
            response = await self.provider.chat_with_retry(...)
            # 成功：记录 token 使用量到 span
            span = tracer.current_span()
            if span is not None:
                span.attributes["tokens_in"] = tokens_in
                span.attributes["tokens_out"] = tokens_out
                span.attributes["tokens_total"] = tokens_total
            return response

        except RecoverableLLMError as exc:
            if recovery_attempt:
                # 已经尝试过一次恢复，放弃
                return self._error_response(exc.info)
            return await self._recover_and_retry(spec, messages, tool_defs, exc.info, ...)

        except RetryableLLMError as exc:
            # Provider 层已重试 3 次仍失败，返回错误响应
            return self._error_response(exc.info)

        except FatalLLMError as exc:
            # 立即返回错误，不做任何恢复尝试
            return self._error_response(exc.info)
```

关键设计：`RecoverableLLMError` 只尝试恢复一次（通过 `recovery_attempt` 标志），防止无限恢复循环。

### 恢复策略分发

```python
async def _recover_and_retry(self, spec, messages, tool_defs, info, *, step_count=0):
    if error_type == "context_length":
        return await self._recover_context_length(spec, messages, tool_defs, info, ...)
    if error_type == "content_filter":
        return await self._recover_content_filter(spec, messages, tool_defs, info, ...)
    return self._error_response(info)
```

### context_length 恢复（两步回退）

```python
async def _recover_context_length(self, spec, messages, tool_defs, info, *, step_count=0):
    # 第一步：按可用策略压缩（优先使用注入的 CompactionService）
    if self.compaction is not None:
        compacted = self.compaction.micro_compact(messages, keep_recent_turns=1)
    else:
        reduced_budget = int(self.max_context_tokens * 0.6)
        compacted = self._lightweight_compact(messages, max_tokens=reduced_budget)
    if _estimate_message_tokens(compacted) < _estimate_message_tokens(messages):
        return await self._call_llm(spec, compacted, tool_defs, recovery_attempt=True, ...)

    # 第二步：丢弃最旧的非系统消息（保留 2/3）
    system_msgs = [m for m in messages if m.get("role") == "system"]
    other_msgs = [m for m in messages if m.get("role") != "system"]
    if len(other_msgs) <= 2:
        return self._error_response(info)  # 已无法进一步裁剪
    keep = max(2, len(other_msgs) * 2 // 3)
    trimmed = system_msgs + other_msgs[-keep:]
    return await self._call_llm(spec, trimmed, tool_defs, recovery_attempt=True, ...)
```

恢复策略分两步：先尝试压缩内容，不行再丢弃旧消息。这比简单截断更温和，尽可能保留上下文。

### content_filter 恢复

```python
async def _recover_content_filter(self, spec, messages, tool_defs, info, *, step_count=0):
    hint = "Ensure all responses comply with safety and content policy guidelines."
    # 在 system prompt 末尾追加合规提示，然后重试
    for i, msg in enumerate(modified):
        if msg.get("role") == "system":
            modified[i] = {**msg, "content": f"{msg['content']}\n\n{hint}"}
            break
    else:
        modified.insert(0, {"role": "system", "content": hint})
    return await self._call_llm(spec, modified, tool_defs, recovery_attempt=True, ...)
```

## 工具执行错误处理

`tools/registry.py:55-78`

ToolRegistry 在执行层面提供统一的错误隔离：

```python
async def execute(self, name, arguments) -> ToolResult:
    tool = self._tools.get(name)
    if tool is None:
        return ToolResult(success=False, content="", error=f"Unknown tool: {name}")

    # ToolGuard 预检查
    if self.guard is not None:
        allowed, reason = self.guard.pre_check(tool.name, tool.capabilities, arguments)
        if not allowed:
            return ToolResult(success=False, content="", error=reason)

    try:
        return await tool.execute(**arguments)
    except Exception as exc:
        return ToolResult(success=False, content="", error=f"Tool '{name}' raised: {exc}")
```

单个工具执行失败不会导致整个 Agent 运行崩溃 — 错误作为 `ToolResult(success=False)` 返回给 LLM，让它自行决定如何处理。

### 并行工具执行的异常处理

`core/runner.py:975-993`

并行工具使用 `asyncio.gather(return_exceptions=True)` 执行，确保一个工具失败不影响其他工具：

```python
tasks = [_exec_one(tc) for _, tc in parallel_group]
raw_results = await asyncio.gather(*tasks, return_exceptions=True)
for (idx, tc), raw in zip(parallel_group, raw_results):
    if isinstance(raw, BaseException):
        result = ToolResult(success=False, content="", error=f"Tool raised: {raw}")
        ...
    else:
        result, duration_ms = raw
```

## 停滞检测

`core/runner.py:245-252`

当 Agent 步数达到 50 步时触发警告：

```python
if step_count == _STALL_WARNING_STEPS:
    logger.warning("Agent reached {} steps — possible stall or infinite loop", step_count)
    await bus.publish(AgentStallWarning(session_key=spec.session_key, step_count=step_count))
```

这不是硬限制，但通过事件总线发布警告，指标系统记录 `agent_stall_warnings_total`。

## 断点恢复 (Checkpoint/Resume)

`core/runner.py:401-511`

Agent 执行循环中，每轮工具执行完成后保存一次检查点，崩溃后可从该点恢复：

### 检查点数据格式

```python
data = {
    "version": 1,
    "session_key": spec.session_key,
    "paradigm": spec.paradigm,
    "step_count": step_count,
    "messages": messages,
    "tools_used": tools_used,
    "tool_events": tool_events,
    "total_usage": total_usage,
    "updated_at": datetime.now(timezone.utc).isoformat(),
}
```

### 生命周期

```
首次运行 → 加载检查点（无）→ 从头开始
每轮工具执行后 → 原子写入 checkpoint 文件（tmp + os.replace）
成功完成 → 删除检查点
耗尽迭代次数 → 删除检查点
异常崩溃 → 检查点保留在磁盘
重新运行 → 加载检查点 → 跳过 on_agent_start 中间件 + AgentStarted 事件 → 从 step N+1 继续
```

### 关键实现

```python
# 保存：原子写入
tmp = path.with_suffix(path.suffix + ".tmp")
tmp.write_text(json.dumps(data, ...), encoding="utf-8")
os.replace(tmp, path)  # 原子替换

# 加载：版本校验 + 完整性检查
if data.get("version") != _CHECKPOINT_VERSION:
    path.unlink(missing_ok=True)  # 丢弃不兼容的检查点
    return None

# 控制：spec.checkpoint 或 MYBOT_CHECKPOINT 环境变量，session_key 为空时自动禁用
@staticmethod
def _checkpointing_enabled(spec):
    if not spec.session_key:
        return False
    if spec.checkpoint:
        return True
    return os.environ.get("MYBOT_CHECKPOINT", "").strip().lower() in ("1", "true", "yes")
```

恢复时跳过 `on_agent_start` 中间件和 `AgentStarted` 事件发布，但 step/llm/tool 中间件钩子正常触发。

## 设计要点

- **三级错误分类**: RETRYABLE → Provider 层退避重试（最多 3 次）；RECOVERABLE → AgentCore 层尝试恢复一次；FATAL → 立即返回错误给用户
- **上下文恢复两步回退**: 先温和压缩，不行再丢弃旧消息，而非一次截断到底
- **工具错误隔离**: 单个工具失败不影响 Agent 循环，错误作为 ToolResult 返回给 LLM
- **检查点设计**: 不保存 spec 参数（model/temperature），恢复时使用新参数，允许重试时更换模型
- **检查点恢复一致性**: 跳过 on_agent_start 事件和中间件，但保留 step/llm/tool 钩子
