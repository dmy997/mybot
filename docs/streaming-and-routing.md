# 流式输出与消息路由 (Streaming & Message Routing)

## 概述

mybot 实现了从 LLM API 到前端浏览器的**端到端真流式输出**：LLM 每产生一个 token 就立刻推送到前端渲染，用户可实时看到生成过程。同时，系统通过 `correlation_id` 和 `session_key` 实现了**多请求多路复用**：同一 session 的多个并发请求不会互相干扰。

## 流式输出完整调用链

```
OpenAI API (SSE stream)
  │  chunk: {"choices":[{"delta":{"content":"你好"}}]}
  │  chunk: {"choices":[{"delta":{"content":"，"}}]}
  │  ...
  │
  ▼
OpenAICompatibleProvider.chat_stream()  (providers/openai_compatible_provider.py:594-667)
  │  body["stream"] = True
  │  stream = await client.chat.completions.create(**body)
  │
  │  async for chunk in stream:
  │    ├── await on_content_delta(content_token)     ← 内容 token
  │    ├── await on_thinking_delta(reasoning_token)  ← 推理 token
  │    └── await on_tool_call_delta(tc_dict)         ← 工具调用 delta
  │
  │  ⚠️ await asyncio.sleep(0)  ← 每个有效 chunk 后让出事件循环 (line 666)
  │                                 确保 outbound 消费者能及时处理
  │
  ▼
LLMProvider.chat_stream_with_retry()  (providers/base.py:138-168)
  │  包装 chat_stream，添加指数退避重试
  │  RetryableLLMError 时重新调用 chat_stream
  │
  ▼
AgentCore._call_llm()  (core/runner.py:677-812)
  │  if spec.on_content_delta or spec.on_tool_call_delta or spec.on_thinking_delta:
  │      response = await provider.chat_stream_with_retry(
  │          on_content_delta=spec.on_content_delta,    ← 直接透传
  │          on_thinking_delta=spec.on_thinking_delta,
  │          on_tool_call_delta=spec.on_tool_call_delta,
  │      )
  │  else:
  │      response = await provider.chat_with_retry(...)  ← 非流式路径
  │
  ▼
═══════════════════════════════════════════════════════════════════════
此处分为两条路径：CLI 直接渲染 vs HTTP/WS 通过 MessageBus 中转
═══════════════════════════════════════════════════════════════════════

┌─── CLI 路径 (tui/app.py) ───────────────────────────────────────────┐
│                                                                      │
│  ChatApp (Textual TUI) 替代了旧版 StreamRenderer 实现：             │
│                                                                      │
│  async def _on_delta(token):                                         │
│      stream.add_token(token)  ← 写入 StreamingMessage 控件          │
│                                                                      │
│  async def _on_tool_start(name, args_brief):                         │
│      # 由 SSE on_tool_call_delta 触发；部分 provider 不支持流式工具调用│
│      pass  # 实际显示由 _on_tool_exec_start 负责                     │
│                                                                      │
│  async def _on_tool_exec_start(name, args, idx, total):              │
│      stream.add_token(f"\n\n● **{name}**({args})\n")                │
│                                                                      │
│  async def _on_tool_exec_end(ev):                                    │
│      if ev["status"] == "error":                                     │
│          stream.add_token(f"  ✗ **{ev['detail']}**\n")               │
│                                                                      │
│  StreamingMessage (tui/widgets.py)                                   │
│    ├── add_token(token) → 累积 + 节流渲染（0.08s / ~12 FPS）        │
│    └── finish() → 最终刷新，完整 Markdown 渲染                       │
└──────────────────────────────────────────────────────────────────────┘

┌─── HTTP/WS 路径 (orchestrator.py:517-564) ──────────────────────────┐
│                                                                      │
│  async def _on_delta(token):                                         │
│      await bus_msg.outbound.put(OutboundMessage(                     │
│          session_key, cid, "delta", token                            │
│      ))                                                              │
│                                                                      │
│  async def _on_thinking(token):                                      │
│      await bus_msg.outbound.put(OutboundMessage(                     │
│          session_key, cid, "thinking", token                         │
│      ))                                                              │
│                                                                      │
│  async def _on_thinking_done():                                      │
│      await bus_msg.outbound.put(OutboundMessage(                     │
│          session_key, cid, "thinking_done", None                     │
│      ))                                                              │
│                                                                      │
│  async def _on_tool_start(name, args_brief=""):                      │
│      await bus_msg.outbound.put(OutboundMessage(                     │
│          session_key, cid, "tool_start", name                        │
│      ))                                                              │
│                                                                      │
│  async def _on_tool_exec_start(name, args, idx, total):              │
│      await bus_msg.outbound.put(OutboundMessage(                     │
│          session_key, cid, "tool_exec_start",                        │
│          {"name": name, "args": args, "index": idx, "total": total}  │
│      ))                                                              │
│                                                                      │
│  async def _on_tool_exec_end(ev):                                    │
│      await bus_msg.outbound.put(OutboundMessage(                     │
│          session_key, cid, "tool_exec_end", ev                       │
│      ))                                                              │
│                                                                      │
│  async def _on_new_turn():                                           │
│      await bus_msg.outbound.put(OutboundMessage(                     │
│          session_key, cid, "new_turn", None                          │
│      ))                                                              │
│                                                                      │
│  # 所有回调 = 构造 OutboundMessage → outbound_queue.put()            │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

### `asyncio.sleep(0)` 的关键作用

`providers/openai_compatible_provider.py:665-666`

```python
if c or r or tc_list:
    await asyncio.sleep(0)
```

因为 `asyncio.Queue.put()` 在队列未满时是同步完成的（不阻塞），没有 `sleep(0)` 的话，provider 会在一次事件循环 tick 中处理完所有 chunk，消费者 task 被饿死，前端看到的是"伪流式"——内容一大块一大块地到达而不是逐 token 渲染。

### 非流式降级

`core/runner.py:707-727`

当 `AgentInput` 没有设置任何流式回调时（如子 agent 内部调用），`_call_llm()` 自动走非流式路径：

```python
if (spec.on_content_delta is not None
        or spec.on_tool_call_delta is not None
        or spec.on_thinking_delta is not None):
    response = await provider.chat_stream_with_retry(...)  # 流式
else:
    response = await provider.chat_with_retry(...)         # 非流式
```

## 消息路由：输入输出如何关联到频道和会话

### 三层标识体系

```
session_key ───── 会话级别：标识一次完整的对话 (e.g. "20260627-143000")
  │                 同一 session 内的所有消息串行处理，共享历史
  │
correlation_id ── 请求级别：标识一次请求-响应完整生命周期 (e.g. "a1b2c3d4e5")
  │                 一个 session 可以有多个并发请求，各自有独立的 cid
  │
source ────────── 来源标识："cli" | "http" | "websocket" | "telegram"
                   用于日志/调试，不影响路由逻辑
```

### 架构：per-session inbound + 共享 outbound

```
                    ┌──────────────────────────────────┐
                    │        MessageBus                 │
                    │                                  │
CLI ───────────────►│ inbound("default")               │←─────── Orchestrator
HTTP POST /chat/s1─►│ inbound("s1")                    │←─────── .serve("s1")
HTTP POST /chat/s2─►│ inbound("s2")                    │←─────── .serve("s2")
WS /ws/s1 ─────────►│ inbound("s1")  ← 共享同一队列    │←─────── .serve("s1")
                    │                                  │
                    │        outbound (共享)            │
Orchestrator ──────►│  OutboundMessage(session, cid, ...)├──────► CLI (全消费)
                    │                                  ├──────► SSE (过滤 cid)
                    │                                  ├──────► WS  (过滤 cid)
                    └──────────────────────────────────┘
```

**关键设计**：
- **inbound**：per-session 独立队列（`asyncio.Queue[InboundMessage]`），一个 session 一个 `serve()` task 消费
- **outbound**：所有 session 共享一个队列（`asyncio.Queue[OutboundMessage]`），消费者按 `correlation_id` 过滤

### InboundMessage — 入站消息

`core/message_bus.py:51-67`

```python
@dataclass
class InboundMessage:
    session_key: str          # ← 路由到哪个 session 的 serve task
    content: str              #   用户输入文本
    source: str               #   "cli" | "http" | "websocket" | "telegram"
    correlation_id: str       #   UUID hex，关联此请求的所有 outbound 消息
    model: str | None         #   可选模型覆盖
    temperature: float | None
    max_tokens: int | None
    goal: str | None
    skills: list[str] | None
    timestamp: float          #   time.monotonic()
```

**路由规则**：`session_key` 决定消息进入哪个 `asyncio.Queue`。`MessageBus.inbound(session_key)` 返回该 session 的专属队列（不存在时自动创建）。Orchestrator.serve() 从对应队列消费。

### OutboundMessage — 出站消息

`core/message_bus.py:71-96`

```python
@dataclass
class OutboundMessage:
    session_key: str           # ← 来源 session（调试用）
    correlation_id: str        # ← 关联到具体请求，消费者按此过滤
    msg_type: str              #   事件类型
    data: Any                  #   事件载荷（按 msg_type 不同）
    timestamp: float           #   time.monotonic()
```

`msg_type` 与 `data` 对应关系：

| msg_type | data 类型 | 说明 |
|----------|----------|------|
| `delta` | `str` | 单个 content token |
| `thinking` | `str` | 单个 reasoning token |
| `thinking_done` | `None` | 推理完成 |
| `tool_start` | `str` | 工具名称 |
| `tool_end` | `dict` | `{name, status, duration_ms, detail}` |
| `tool_exec_start` | `dict` | `{name, args, index, total}` |
| `tool_exec_end` | `dict` | 工具事件详情 |
| `final` | `dict` | `{content, usage, stop_reason, paradigm}` |
| `error` | `str` | 错误信息 |

### 完整请求生命周期（含流式事件流）

```
一次 HTTP POST /chat/session_abc 请求的完整时间线

1. POST 到达 → chat_sse()  (server.py:128)
   │
2. 生成 cid = uuid4().hex  ("a1b2c3d4e5")
   │
3. _ensure_serve_task("session_abc")  (server.py:114-119)
   │  检查是否有活跃的 serve task，没有则创建：
   │  asyncio.create_task(orchestrator.serve(bus_msg, "session_abc"))
   │
4. 入站：bus_msg.inbound("session_abc").put(InboundMessage(
        session_key="session_abc",
        content="你好",
        source="http",
        correlation_id="a1b2c3d4e5",
    ))                                   (server.py:149-156)
   │
5. Orchestrator.serve() 后台 task（从步骤 3 开始运行） (orchestrator.py:485-580)
   │  inbound = await bus_msg.inbound("session_abc").get()
   │  取出 InboundMessage
   │
   │  构造回调 (orchestrator.py:517-547)：
   │    _on_delta(token)        → outbound.put(OutboundMessage(..., "delta", token))
   │    _on_thinking(token)     → outbound.put(OutboundMessage(..., "thinking", token))
   │    _on_tool_start(name)    → outbound.put(OutboundMessage(..., "tool_start", name))
   │    _on_tool_end(ev)        → outbound.put(OutboundMessage(..., "tool_end", ev))
   │    _on_tool_exec_start(...) → outbound.put(OutboundMessage(..., "tool_exec_start", ...))
   │    _on_tool_exec_end(ev)   → outbound.put(OutboundMessage(..., "tool_exec_end", ev))
   │
   │  result = await process_message(
   │      session_key, content,
   │      on_delta=_on_delta,
   │      on_tool_start=_on_tool_start, ...
   │  )                                (orchestrator.py:550-565)
   │
   │  发送最终消息 (orchestrator.py:566-575)：
   │  outbound.put(OutboundMessage(..., "final", {
   │      content, usage, stop_reason, paradigm, elapsed_ms
   │  }))
   │
   │  循环回步骤 5，等待下一条入站消息
   │
6. event_stream() SSE 消费者（步骤 1 返回的 StreamingResponse） (server.py:146-198)
   │  while True:
   │    out = await bus_msg.outbound.get()
   │    if out.correlation_id != "a1b2c3d4e5": continue  ← 过滤其他请求
   │    if out.msg_type == "delta":       → yield SSE event: delta
   │    if out.msg_type == "thinking":    → yield SSE event: thinking
   │    if out.msg_type == "tool_start":  → yield SSE event: tool_start
   │    if out.msg_type == "tool_end":    → yield SSE event: tool_end
   │    if out.msg_type == "final":       → yield SSE event: done → break
   │
7. 浏览器逐 token 渲染
```

### 多请求并发路由示例

同一 session "s1" 同时有两个 HTTP 请求进行中：

```
时间 ──────────────────────────────────────────────────────►

请求 A (cid="aaa"):
  POST → inbound("s1").put(InboundMessage(cid="aaa", "写代码"))
    → serve("s1") 从队列取出消息 A
    → process_message() → LLM 流式输出
      → outbound: (cid="aaa", "delta", "def"), (cid="aaa", "delta", " main")...

请求 B (cid="bbb"):
  POST → inbound("s1").put(InboundMessage(cid="bbb", "搜索资料"))
    → 等待请求 A 的 process_message() 完成（同一 session 串行消费）
    → serve("s1") 从队列取出消息 B
    → process_message() → LLM 流式输出
      → outbound: (cid="bbb", "delta", "搜索结果")...

前端 A (cid="aaa") 的 SSE consumer:
  out = outbound_queue.get()
  if out.correlation_id == "aaa": 渲染 ✓    ← 过滤掉 cid="bbb" 的消息
  if out.correlation_id == "bbb": 跳过 ✗

前端 B (cid="bbb") 的 SSE consumer:
  out = outbound_queue.get()
  if out.correlation_id == "aaa": 跳过 ✗
  if out.correlation_id == "bbb": 渲染 ✓    ← 过滤掉 cid="aaa" 的消息
```

关键保证：
- 同一 session 内请求**串行处理**（单队列 → 单 `serve()` 消费者），保证会话状态一致性
- 不同请求的输出通过 `correlation_id` 在共享 outbound 队列上**多路复用**
- 每个 consumer 只取自己的消息，跳过其他消息（消息不会丢失——其他 consumer 会读到）

### 不同来源的消费者对比

| 来源 | 入站方式 | 出站消费方式 | 过滤逻辑 | 特殊能力 |
|------|---------|-------------|---------|---------|
| **CLI** | `orchestrator.process_message()` 同步调用 | 无 MessageBus，回调直接更新 StreamingMessage（tui/widgets.py） | 无需过滤（单请求） | 直接渲染，零延迟 |
| **HTTP SSE** | `POST /chat/{sid}` → inbound.put() | `async for` 读 outbound 队列 | `out.correlation_id != cid` → skip | 流式响应 |
| **WebSocket** | `{"type":"chat"...}` → inbound.put() | `async for` 读 outbound 队列 | `out.correlation_id != cid` → skip | 支持 cancel（取消 in-flight task） |

## 设计要点

- **`asyncio.sleep(0)` 防饿死**：每个 chunk 后让出事件循环，确保消费者能及时渲染，避免"伪流式"
- **非流式降级**：`AgentInput` 未设置回调时自动走 `chat_with_retry`，节省非用户场景的开销
- **session_key 路由入站**：per-session 独立队列，一个 session 的慢请求不阻塞其他 session
- **correlation_id 路由出站**：共享 outbound 队列，消费者按 cid 过滤，实现多路复用
- **同一 session 串行**：`serve()` 单消费者串行处理，避免并发修改会话状态
- **CLI vs HTTP 双路径**：CLI 直接渲染（最低延迟），HTTP/WS 通过 MessageBus 解耦（支持多消费者）
- **去重工具调用回调**：CLI 路径中 `_shown_tool_indices` 确保同一 tool_call delta 只触发一次 tool_start 事件
