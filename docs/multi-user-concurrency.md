# 多用户并发处理 (Multi-User Concurrency)

## 概述

mybot 通过 **per-session serve task + MessageBus 解耦** 实现多用户并发。每个会话（session）由一个独立的后台 `asyncio.Task`（`serve()` 协程）串行消费入站消息，不同会话之间完全并行。出站消息通过共享队列 + `correlation_id` 过滤实现请求级多路复用。

核心设计原则：**同一 session 内请求串行 → 保证会话状态一致性；不同 session 间完全并行 → 互不阻塞**。

## 架构总览

```
                              ┌──────────────────────────────────────────┐
                              │            MessageBus                     │
                              │                                          │
  HTTP POST /chat/s1 ────────►│  inbound("s1")  [Queue maxsize=64]       │
  WS /ws/s1 ────────────────►│  inbound("s1")  ← 同一队列，共享         │
  HTTP POST /chat/s2 ────────►│  inbound("s2")  [Queue maxsize=64]       │
  WS /ws/s2 ────────────────►│  inbound("s2")                           │
                              │                                          │
                              │  outbound("http")     [Queue maxsize=256]│
                              │    ├── (s1, cid=aaa, "delta", "你好")     │
                              │    ├── (s2, cid=ccc, "delta", "Hello")   │
                              │  outbound("websocket")                   │
                              │    ├── (s1, cid=bbb, "tool_start", ...)  │
                              │    └── ...                               │
                              └──────┬───────────────────────────────────┘
                                     │
         ┌───────────────────────────┼───────────────────────────┐
         │                           │                           │
         ▼                           ▼                           ▼
  Orchestrator.serve("s1")   Orchestrator.serve("s2")   Orchestrator.serve("s3")
  (asyncio.Task)             (asyncio.Task)             (asyncio.Task)
         │                           │                           │
         ▼                           ▼                           ▼
  process_message()          process_message()          process_message()
         │                           │                           │
         ▼                           ▼                           ▼
  SSE consumer (cid=aaa)     SSE consumer (cid=ccc)     WS consumer (cid=ddd)
  reads outbound("http")     reads outbound("http")    reads outbound("websocket")
```

## 并发模型对比

| 维度 | CLI 模式 | HTTP/WS 模式 |
|------|---------|-------------|
| 并发会话数 | 1（单用户） | 无限制 |
| 消息传递 | 直接调用 `process_message()` | 通过 MessageBus 异步解耦 |
| 流式回调 | 直接更新 StreamingMessage widget（Textual reactive） | 转为 OutboundMessage → 共享队列 → 消费者过滤 |
| 会话隔离 | N/A（仅一个会话） | per-session serve task 串行消费 |
| 请求并发 | N/A（同步交互循环） | 同 session 内串行，不同 session 间并行 |

## MessageBus：入站与出站队列

`core/message_bus.py:104-161`

### 入站队列（per-session）

```python
# core/message_bus.py:116-133
class MessageBus:
    def __init__(self, outbound_maxsize: int = 256, inbound_maxsize: int = 64):
        self._outbound: dict[str, asyncio.Queue[OutboundMessage]] = {}
        self._outbound_maxsize = outbound_maxsize
        self._inbound: dict[str, asyncio.Queue[InboundMessage]] = {}
        self._inbound_maxsize = inbound_maxsize

    def inbound(self, session_key: str) -> asyncio.Queue[InboundMessage]:
        """返回 session 专属的入站队列，不存在时自动创建（maxsize=64）。"""
        if session_key not in self._inbound:
            self._inbound[session_key] = asyncio.Queue(maxsize=self._inbound_maxsize)
        return self._inbound[session_key]
```

- **每个 session 独立队列**：`inbound("s1")` 和 `inbound("s2")` 是不同的 `asyncio.Queue` 实例
- **maxsize=64**：队列满时生产者（HTTP/WS handler）会被阻塞，形成背压
- **惰性创建**：队列在首次 `inbound(session_key)` 调用时创建
- **生命周期**：`remove_session(session_key)` 删除队列引用；`close()` 向所有队列放入 `None` 哨兵

### 出站队列（per-channel）

```python
# core/message_bus.py:122-133
self._outbound: dict[str, asyncio.Queue[OutboundMessage]] = {}
self._outbound_maxsize = outbound_maxsize

def outbound(self, channel: str = "default") -> asyncio.Queue[OutboundMessage]:
    if channel not in self._outbound:
        self._outbound[channel] = asyncio.Queue(maxsize=self._outbound_maxsize)
    return self._outbound[channel]
```

- **per-channel 独立**：每个频道（`"cli"`、`"http"`、`"websocket"`、`"wechat"`）有独立的出站队列，惰性创建
- **maxsize=256**：每个队列独立计容量，单频道满不影响其他频道的出站
- **按 `source` 路由**：`orchestrator.serve()` 从 `InboundMessage.source` 提取 channel，消息路由到对应队列
- **消费者不再丢弃消息**：每个 consumer 只读自己频道的队列，不会收到其他频道的消息

### 消息数据结构

```python
# core/message_bus.py:51-67
@dataclass
class InboundMessage:
    session_key: str          # 目标会话 → 路由到哪个 inbound 队列
    content: str              # 用户输入文本
    source: str               # "cli" | "http" | "websocket" | "telegram"
    correlation_id: str       # UUID hex，关联此请求的所有 outbound 消息
    model: str | None
    temperature: float | None
    max_tokens: int | None
    goal: str | None
    skills: list[str] | None
    timestamp: float          # time.monotonic()

# core/message_bus.py:71-96
@dataclass
class OutboundMessage:
    session_key: str           # 来源 session（调试用）
    correlation_id: str        # 关联到具体请求，消费者按此过滤
    msg_type: str              # delta|thinking|thinking_done|tool_start|tool_end|
                               #   tool_exec_start|tool_exec_end|final|error
    data: Any                  # 按 msg_type 不同
    timestamp: float
```

## 请求生命周期：从入站到出站

### 完整调用链

```
═══════════════════════════════════════════════════════════════════════════
1. 客户端发起请求
═══════════════════════════════════════════════════════════════════════════

HTTP: POST /chat/{session_id}  →  chat_sse()           (server.py:159)
WS:   {"type":"chat", ...}     →  ws_endpoint()        (server.py:284)

   ├── 认证检查: _check_auth(request) / _ws_check_auth(headers)
   │     MYBOT_API_KEY 为空 → 跳过认证
   │     MYBOT_API_KEY 设置 → 校验 Authorization: Bearer <key>
   │
   ├── 生成 correlation_id = uuid4().hex                  (server.py:176)
   │
   ├── _ensure_serve_task(session_id)                     (server.py:118-131)
   │     if session_id not in _serve_tasks or _serve_tasks[session_id].done():
   │         _serve_tasks[session_id] = asyncio.create_task(
   │             orchestrator.serve(bus_msg, session_id)
   │         )
   │
   └── bus_msg.inbound(session_id).put(InboundMessage(
           session_key=session_id,
           content=user_message,
           source="http" | "websocket",
           correlation_id=cid,
           model=..., temperature=...,
       ))                                     (server.py:181-189)

═══════════════════════════════════════════════════════════════════════════
2. Orchestrator.serve() 消费入站消息
═══════════════════════════════════════════════════════════════════════════

Orchestrator.serve(bus_msg, session_key)                 (orchestrator.py:588-703)

  while self._running:
      inbound = await bus_msg.inbound(session_key).get()   # 阻塞等待
      │                                                      # 1s 超时做 idle check
      │
      ├── channel = msg.source or "default"  ← 从入站消息提取频道
      │
      ├── 构造流式回调（_on_delta, _on_thinking, ...）
      │     _safe_put(OutboundMessage(...), channel)
      │       → bus_msg.outbound(channel).put_nowait(...)
      │
      ├── result = await self.process_message(
      │       session_key, user_input, ...,
      │       on_delta=_on_delta,
      │       ...
      │   )
      │
      └── _safe_put(OutboundMessage(..., "final", {...}), channel)

═══════════════════════════════════════════════════════════════════════════
3. 消费者从共享 outbound 队列读取
═══════════════════════════════════════════════════════════════════════════

SSE event_stream():                                      (server.py:178-236)
  while True:
      out = await bus_msg.outbound("http").get()   ← 只读 HTTP 频道
      if out.correlation_id != cid: continue       ← 过滤同频道其他请求
      if out.msg_type == "delta":     → SSE event: delta
      if out.msg_type == "thinking":  → SSE event: thinking
      if out.msg_type == "tool_start":→ SSE event: tool_start
      if out.msg_type == "tool_end":  → SSE event: tool_end
      if out.msg_type == "final":     → SSE event: done → break
      if out.msg_type == "error":     → SSE event: error → break

WS _run():                                               (server.py:301-360)
  while True:
      out = await bus_msg.outbound("websocket").get()  ← 只读 WS 频道
      if out.correlation_id != cid: continue
      ... → ws.send_json({...})
```

### 多请求并发路由示例

同一 session "s1" 同时有两个 HTTP 请求：

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
  if out.correlation_id == "aaa": 渲染 ✓
  if out.correlation_id == "bbb": 跳过 ✗

前端 B (cid="bbb") 的 SSE consumer:
  out = outbound_queue.get()
  if out.correlation_id == "aaa": 跳过 ✗
  if out.correlation_id == "bbb": 渲染 ✓
```

关键保证：
- **同一 session 内串行**：`serve()` 单消费者从 per-session 队列逐一取出消息，保证会话状态一致性
- **不同 session 间并行**：`serve("s1")` 和 `serve("s2")` 是不同的 `asyncio.Task`，在事件循环中并发执行
- **请求级多路复用**：共享 outbound 队列 + `correlation_id` 过滤实现多个消费者各自取自己的消息

## Serve Task 生命周期管理

`core/server.py:118-131`

```python
# 模块级字典（create_app() 闭包内）
_serve_tasks: dict[str, asyncio.Task[None]] = {}

async def _ensure_serve_task(session_key: str) -> None:
    if session_key not in _serve_tasks or _serve_tasks[session_key].done():
        _serve_tasks[session_key] = asyncio.create_task(
            orchestrator.serve(bus_msg, session_key)
        )
```

- **惰性启动**：首次请求到达时才创建 serve task，而非服务器启动时
- **自动恢复**：若 serve task 因异常崩溃，`done()` 检查会在下次请求时自动重建
- **无主动清理**：会话删除时 serve task 不会被取消——`delete_session()` 只删除持久化文件，serve task 继续运行并在下次入站消息到达时继续处理
- **哨兵关闭**：`MessageBus.close()` 向所有入站队列放入 `None`，serve task 收到后退出循环；但这发生在整个 MessageBus 关闭时，而非单个会话级别

### serve() 内部循环

`core/orchestrator.py:541-656`

```python
async def serve(self, bus_msg: MessageBus, session_key: str):
    inbound = bus_msg.inbound(session_key)
    self._running = True
    try:
        while self._running:
            try:
                msg = await asyncio.wait_for(inbound.get(), timeout=1.0)
            except asyncio.TimeoutError:
                await self._compress_idle_sessions(session_key)
                continue
            if msg is None:  # 哨兵 → 退出
                break
            # ... 处理消息 → 流式回调 → outbound.put(...) ...
    finally:
        self._running = False
```

⚠️ **已知问题**：`self._running` 是 Orchestrator **实例级**共享变量。多个 serve task 共享同一个 Orchestrator 实例，因此当中任意一个 serve task 退出时（例如收到 `None` 哨兵），`finally` 块将 `self._running` 设为 `False`，导致**所有其他 serve task 的 `while self._running` 循环也一并退出**。这是一个待修复的并发缺陷。

## 并发安全机制

### Per-Session 写锁

`context/session.py:45, 102-115`

```python
class SessionManager:
    def __init__(self):
        self._write_locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def lock_session(self, key: str):
        lock = self._get_write_lock(key)
        async with lock:
            yield
```

每次写入会话数据（保存消息、更新 cursor、压缩）时获取该锁。这确保同一 session 的并发写操作不会损坏数据。

**使用位置**：

| 调用位置 | 文件:行号 | 触发场景 |
|---------|----------|---------|
| `ContextManager.save_exchange()` | `context_manager.py:473` | 每次 process_message() 成功后 |
| `ContextManager.save_session()` | `context_manager.py:491` | 替换整个消息列表 |
| `CompactionService.auto_compact()` | `compaction.py:187` | 空闲/运行时压缩 |
| `CompactionService.full_compact()` | `compaction.py:276` | 用户触发压缩 |
| `Orchestrator.process_message()` | `orchestrator.py:379,389` | CancelledError/KeyboardInterrupt 时保存部分状态 |

### 其他锁机制

| 锁 | 位置 | 粒度 | 作用 |
|----|------|------|------|
| SessionManager._write_locks | `session.py:45` | per-session | 保护会话数据写入 |
| Consolidator._locks | `consolidator.py:70` | per-session | 防止重复合并 |
| Provider._client_lock | `openai_compatible_provider.py:64` | 全局（单例） | 保护 AsyncOpenAI 客户端惰性初始化 |
| CronScheduler._locks | `cron.py:78` | per-job | 防止 cron job 并发执行 |
| EventBus._lock | `events.py:190` | 全局 | 保护订阅者列表修改 |

### 读取不加锁

`context/session.py:61-63`

```python
def get_session_history(self, key: str) -> list[dict]:
    session = self.sessions.get(key)
    return list(session.messages) if session else []
```

读取操作（`get_session_history`、`list_sessions`）不获取锁，直接返回列表的浅拷贝。这在写入并发时可能导致读取到中间状态，但实践中写入是瞬时的（JSON 序列化），风险很低。

### 会话隔离

- **无会话所有权**：任何知道 `session_id` 的客户端都可以访问该会话。会话密钥是 URL 路径参数，没有访问控制
- **认证可选**：`MYBOT_API_KEY` 为空时所有请求都允许；设置为 Bearer token 校验
- **数据隔离**：每个 session 的数据存储在独立的 JSON 文件中（`workspace/sessions/{session_key}.json`）

## 背压（Backpressure）机制

系统通过队列容量上限实现隐式背压，**没有显式的速率限制或全局并发信号量**：

```
入站方向（HTTP/WS → Orchestrator）:
  inbound queue (maxsize=64) 满 → HTTP handler 的 put() 被阻塞
  → uvicorn 的连接池自然形成背压

出站方向（Orchestrator → 客户端）:
  outbound(channel) queue (maxsize=256) 满 → serve() 中的 _safe_put() 丢弃消息
  → LLM token 生成继续（非阻塞），但单频道丢消息时有 warning 日志
  → 使用 put_nowait() 避免 deadlock；消费者断开时消息被丢弃而非阻塞整个 serve()
```

这是唯一的内置流控机制。系统**没有**：
- `asyncio.Semaphore` 限制最大并发请求数
- 速率限制器（rate limiter）
- 最大并发 session 数限制

## 共享 Outbound 队列的竞态条件（已解决）

> **状态：已通过 per-channel outbound 队列解决。** 此节保留作为历史记录和设计演进参考。

旧架构中 `core/server.py:192-197` 的代码：

```python
# 旧：所有消费者都从同一个共享队列读取
while True:
    out = await bus_msg.outbound.get()
    if out.correlation_id != cid:
        continue  # 不是本请求的消息，跳过
```

**旧问题**：当消费者 A 从队列取出消息，发现 `correlation_id` 不匹配而跳过时，该消息已经**丢失**（从队列中移除）。如果消费者 B（本该接收此消息）尚未轮到读取，它将永远收不到该消息。

**解决方案**：将 `outbound` 从单个共享队列改为 per-channel 独立队列：

```python
# 新：每个频道独立队列，consumer 只读自己频道的队列
while True:
    out = await bus_msg.outbound("http").get()  # ← 只读 HTTP 频道的队列
    if out.correlation_id != cid:
        continue  # 同频道内仍需过滤（一个频道可有多个并发请求）
```

- 不同频道的消息完全隔离——SSE consumer 永远不会读到 WS 的消息
- 同频道内的多请求仍通过 `correlation_id` 过滤（如同频道两个 HTTP 请求）
- `orchestrator.serve()` 从 `InboundMessage.source` 提取频道并路由到正确队列
- WeChatBot consumer 读取 `outbound("wechat")`，只接收自己的消息

## WebSocket 的请求取消

`core/server.py:373-388`

```python
# ws_endpoint() 中
if msg_type == "chat":
    if _current_task and not _current_task.done():
        _current_task.cancel()  # 取消正在进行的请求
    _current_task = asyncio.create_task(_run(message, model, temperature))
```

WebSocket 连接支持**请求取消**：
- 新的 `chat` 消息自动取消前一个未完成的请求
- 显式的 `cancel` 消息取消当前请求
- `WebSocketDisconnect` 时取消当前请求

这允许用户在 WebSocket 通道中中断长时间运行的 Agent 操作。SSE 端点没有此能力（HTTP 请求-响应模型）。

## 设计要点

- **per-session serve task**：每个 session 一个后台 `asyncio.Task`，惰性创建，自动恢复
- **per-session inbound 队列**：不同 session 的入站消息完全隔离，慢 session 不阻塞快 session
- **同一 session 串行**：单消费者保证会话状态一致性，避免并发写入冲突
- **per-channel outbound 队列 + correlation_id 过滤**：频道间完全隔离，频道内请求级多路复用。不再丢弃非当前 cid 的消息
- **per-session 写锁**：`SessionManager.lock_session()` 保护数据写入，防止损坏
- **队列容量背压**：`inbound_maxsize=64` + `outbound_maxsize=256` 形成隐式流控；`put_nowait()` 防 deadlock
- **WebSocket 请求取消**：支持中断长时间运行的 Agent 操作
- **无全局并发限制**：没有信号量、速率限制器或最大 session 数限制
- **CLI vs HTTP vs WeChat 多模型**：CLI 同步调用（零延迟），HTTP/WS/WeChat 通过 MessageBus 解耦（每通道独立队列）
