# Web 聊天界面 (Web Chat UI)

## 概述

mybot 提供一个完整的 Web 聊天界面，通过 `mybot-server` 命令启动。后端基于 Starlette（HTTP SSE + WebSocket），前端是单个 HTML 文件（`server_web/index.html`，约 410 行），使用原生 JavaScript 实现，零构建步骤。

启动后访问 `http://127.0.0.1:8080` 即可使用。

## 架构总览

```
浏览器 (index.html)
  │
  ├── POST /chat/{session_id}  ───────── SSE 流式响应 (chat_sse)
  ├── GET  /sessions           ───────── JSON 会话列表
  ├── GET  /sessions/{id}      ───────── JSON 单个会话
  ├── DELETE /sessions/{id}    ───────── 删除会话
  └── WS   /ws/{session_id}    ───────── WebSocket 双向通信 (备用)

服务端:
  Starlette app (core/server.py:create_app)
    │
    ├── chat_sse:       POST → InboundMessage → bus_msg.inbound.put()
    │                   → 等待 OutboundMessage → 转为 SSE event → StreamingResponse
    │
    ├── ws_endpoint:    WebSocket → InboundMessage → bus_msg.inbound.put()
    │                   → 等待 OutboundMessage → 转为 JSON → websocket.send_json()
    │
    ├── index:           返回 server_web/index.html (首次读取后缓存)
    │
    └── _ensure_serve_task:
        启动 Orchestrator.serve() 后台 Task，按需创建，每 session 一个
```

## 关键模块

| 模块 | 文件 | 职责 |
|------|------|------|
| Server App | `core/server.py:84-389` | Starlette 路由注册、SSE/WS 端点实现 |
| Entry Point | `core/server.py:396-437` | uvicorn 启动、读取 `MYBOT_HOST`/`MYBOT_PORT` 环境变量 |
| MessageBus | `core/message_bus.py` | 解耦 I/O：InboundMessage (入站) + OutboundMessage (出站) |
| Frontend UI | `server_web/index.html` | 单文件 HTML + CSS + JS，marked.js CDN 渲染 Markdown |

## 前端 UI 详解

`server_web/index.html` 是一个完整的单文件聊天应用，包含：

### 布局结构

```
┌──────────────┬──────────────────────────────────┐
│   Sidebar    │   Main                           │
│   (260px)    │                                  │
│              │   Chat Header (session name)      │
│   Session    ├──────────────────────────────────┤
│   List       │   Messages Area                   │
│   (flex)     │   - User bubbles (right, blue)    │
│              │   - Assistant bubbles (left, gray)│
│     + New    │     - Think blocks (collapsible)  │
│     Session  │     - Tool blocks (collapsible)   │
│     Button   │   - Usage badges                  │
│              ├──────────────────────────────────┤
│              │   Input Area [text input] [Send]  │
└──────────────┴──────────────────────────────────┘
```

### 视觉特性

- **暗色主题**：CSS 变量定义 10+ 颜色令牌（`--bg`, `--surface`, `--accent` 等）
- **可折叠区块**：Thinking 和 Tool 调用以折叠块形式展示，点击标题切换展开/折叠
- **Markdown 渲染**：通过 `marked.js` CDN 将助手回复渲染为 HTML（支持代码块、列表等）
- **加载动画**：CSS 三点跳动动画，流式输出期间显示
- **自适应气泡**：用户消息右对齐（蓝色）、助手消息左对齐（灰色边框）、错误消息居中（红色）

### 核心 JS 逻辑

```javascript
// 会话管理
loadSessions()        // GET /sessions → 填充侧边栏
selectSession(key)    // 切换活跃会话
newSession()          // prompt() 输入名称 → 创建会话
deleteSession(key)    // DELETE /sessions/{key} → 刷新列表

// 聊天流程
sendMessage()         // 核心函数 (index.html:225-360)
  ├── addBubble('user', message)               // 添加用户气泡
  ├── addBubble('assistant', '')               // 添加助手占位气泡
  ├── fetch('/chat/' + session, { POST })       // 发起 SSE 请求
  └── reader.read() 循环解析 SSE 事件
        ├── event: delta        → 累积 token → marked.parse() 渲染
        ├── event: thinking     → 创建/追加 thinking 折叠块
        ├── event: thinking_done → 2 秒后自动折叠
        ├── event: tool_start   → 创建 tool 折叠块（绿色标题）
        ├── event: tool_end     → 追加 tool 执行结果
        ├── event: done         → 显示 token 用量徽章
        └── event: error        → 显示红色错误信息
```

### SSE 事件流解析

前端手动解析 SSE 流（而非使用 `EventSource`，因为 `EventSource` 不支持 POST）：

```javascript
// index.html:264-351
const reader = resp.body.getReader();
const decoder = new TextDecoder();
let buf = '';

while (true) {
    const { done, value } = await reader.read();
    buf += decoder.decode(value, { stream: true });

    // 按 \n\n 分割 SSE 事件
    const parts = buf.split('\n\n');
    buf = parts.pop(); // 保留不完整部分（可能跨 chunk）

    for (const part of parts) {
        // 解析 "event: xxx" 和 "data: {...}" 行
        // 分发到对应的事件处理逻辑
    }
}
```

## 服务端详解

### 启动流程

```
mybot-server 命令
  └── core/server.py:main()  (line 396-437)
        ├── 读取环境变量 MYBOT_HOST (默认 127.0.0.1), MYBOT_PORT (默认 8080)
        ├── 创建 OpenAICompatibleProvider (读取 API key/base/name/model)
        ├── 创建 Orchestrator(workspace, provider, compress_model)
        │     └── Orchestrator.__init__()  → 创建 ContextManager, Dispatcher,
        │         ToolRegistry, CronScheduler, Dream
        │
        ├── create_app(orchestrator) → Starlette app  (line 84-389)
        │     └── 注册路由 (见下方路由表)
        │
        ├── orchestrator.start_services()  # 启动 MCP + Cron
        └── uvicorn.Server(app).serve()
```

### 路由表

`core/server.py:379-387`

| 方法 | 路径 | 处理函数 | 说明 |
|------|------|----------|------|
| `GET` | `/` | `index()` | 返回 `server_web/index.html`（首次读取后缓存为 bytes） |
| `GET` | `/health` | `health()` | 健康检查，返回 `{"status": "ok"}` |
| `POST` | `/chat/{session_id}` | `chat_sse()` | SSE 流式聊天：发送消息 → 接收流式响应 |
| `GET` | `/sessions` | `list_sessions()` | 列出所有会话（JSON 数组） |
| `GET` | `/sessions/{session_id}` | `get_session()` | 获取单个会话详情 |
| `DELETE` | `/sessions/{session_id}` | `delete_session()` | 删除会话 |
| `WS` | `/ws/{session_id}` | `ws_endpoint()` | WebSocket 双向通信（备用通道） |

### 认证机制

`core/server.py:55-76`

```
_check_auth(request):
  MYBOT_API_KEY 为空 → 允许所有请求 (auth disabled)
  MYBOT_API_KEY 设置 → 校验 Authorization: Bearer <key>

_ws_check_auth(headers):
  同上，从 WebSocket headers 中提取 Authorization
```

### 请求处理完整调用链

```
1. 用户在前端输入消息，点击 Send

2. 前端: fetch POST /chat/{session_id}  (index.html:252-256)
     body: {"message": "你好"}

3. Server: chat_sse()  (core/server.py:128-207)
     │
     ├── _check_auth(request)  → 校验 Bearer token
     │
     ├── 解析 request body → message, model, temperature
     │   生成 correlation_id = uuid4().hex  (line 144)
     │   # 用于关联此请求的所有 outbound 消息
     │
     ├── _ensure_serve_task(session_id)  (line 114-119)
     │   # 首次请求时创建 Orchestrator.serve() 作为后台 asyncio.Task
     │   # serve() 是一个长时间运行的循环，持续从 inbound 队列读消息
     │   # 每个 session 最多一个 serve task（懒启动）
     │
     ├── bus_msg.inbound(session_id).put(InboundMessage(
     │       session_key=session_id,
     │       content=message,
     │       source="http",
     │       correlation_id=cid,
     │       model=model,
     │       temperature=temperature,
     │   ))                                    (line 149-156)
     │   # 将用户消息放入该 session 的入站队列
     │
     └── async def event_stream():  ← 返回为 StreamingResponse
           │                              (line 199-207)
           │  Orchestrator.serve() 从 inbound 队列取出消息 → 处理
           │  → 调用 process_message() → 执行 agent → 产生流式输出
           │  → 每个流式事件作为 OutboundMessage 放入 outbound 队列
           │
           └── while True:                    (line 159-195)
                 out = await bus_msg.outbound.get()
                 if out.correlation_id != cid: continue  # 过滤非本请求的消息
                 │
                 ├── out.msg_type == "delta"         → SSE event: delta
                 ├── out.msg_type == "thinking"       → SSE event: thinking
                 ├── out.msg_type == "thinking_done"  → SSE event: thinking_done
                 ├── out.msg_type == "tool_start"     → SSE event: tool_start
                 ├── out.msg_type == "tool_end"       → SSE event: tool_end
                 ├── out.msg_type == "tool_exec_start"→ SSE event: tool_exec_start
                 ├── out.msg_type == "tool_exec_end"  → SSE event: tool_exec_end
                 ├── out.msg_type == "final"          → SSE event: done → break
                 └── out.msg_type == "error"          → SSE event: error → break

4. 前端: 解析 SSE 事件流 → 逐步渲染到页面  (index.html:290-349)
```

### Orchestrator.serve() 消息处理循环

`core/orchestrator.py:485-580`

```
Orchestrator.serve(bus_msg, session_key):
  │
  └── while self._running:
        │
        ├── inbound = await bus_msg.inbound(session_key).get()
        │   # 阻塞等待用户消息（1s 超时做 idle compression 扫描）
        │
        ├── 根据 InboundMessage 构造回调（_on_delta, _on_thinking, _on_tool_start 等）
        │
        ├── result = await self.process_message(
        │       session_key, user_input, model, temperature, ...,
        │       on_delta=_on_delta,      # 每个 content token → OutboundMessage("delta")
        │       on_thinking=_on_thinking, # 每个 thinking token → OutboundMessage("thinking")
        │       on_tool_start=_on_tool_start,     # 工具开始 → OutboundMessage("tool_start")
        │       on_tool_execute_start=_on_tool_exec_start,  # 工具执行开始
        │       on_tool_execute_end=_on_tool_exec_end,      # 工具执行结束
        │   )
        │   # 所有回调内部将事件转为 OutboundMessage 放入 outbound 队列
        │
        └── 发送最终消息:
              OutboundMessage("final", {
                  "content": result.content,
                  "usage": result.usage,
                  "stop_reason": result.stop_reason,
                  "paradigm": result.paradigm,
                  "elapsed_ms": ...
              })
```

### SSE 事件格式

`core/server.py:40-47`

```python
def _sse_event(event: str, data=None) -> str:
    """格式化 SSE 事件字符串。

    输出格式:
      event: delta\n
      data: {"token": "你好"}\n
      \n
    """
    return f"event: {event}\n" + f"data: {json.dumps(data)}\n\n"
```

响应头设置 (`core/server.py:200-207`)：
- `Content-Type: text/event-stream` — SSE 媒体类型
- `Cache-Control: no-cache` — 禁用浏览器缓存
- `Connection: keep-alive` — 保持 TCP 连接
- `X-Accel-Buffering: no` — 禁用 nginx 代理缓冲（确保 token 实时送达）

### WebSocket 端点

`core/server.py:238-358`

WebSocket 端点（`/ws/{session_id}`）提供与 SSE 端点等价的功能，但使用双向 JSON 消息：

```
客户端 → 服务端:
  {"type": "chat", "message": "你好", "model": "...", "temperature": 0.7}
  {"type": "cancel"}

服务端 → 客户端:
  {"type": "delta", "token": "..."}
  {"type": "thinking", "token": "..."}
  {"type": "thinking_done"}
  {"type": "tool_start", "name": "bash"}
  {"type": "tool_end", "name": "bash", "status": "ok", ...}
  {"type": "done", "content": "...", "usage": {...}}
  {"type": "error", "message": "..."}
```

WebSocket 额外支持**请求取消**：新的 `chat` 消息会取消当前正在进行的 task (`_current_task.cancel()`)，允许用户中断长时间运行的 Agent 操作。

## MessageBus 解耦机制

`core/message_bus.py`

MessageBus 是服务端 I/O 的核心解耦层，包含两类队列：

```
Inbound (per-session asyncio.Queue[InboundMessage]):
  maxsize = 64
  生产者: HTTP handler (chat_sse) / WS handler (ws_endpoint)
          → InboundMessage(session_key, content, source, correlation_id, model, ...)
  消费者: Orchestrator.serve()
          → 从队列取出消息 → 调用 process_message()

Outbound (共享 asyncio.Queue[OutboundMessage]):
  maxsize = 256
  生产者: Orchestrator.serve()  → 流式回调产生 OutboundMessage
          → OutboundMessage(session_key, correlation_id, msg_type, data)
  消费者: HTTP SSE event_stream / WS connection
          → 按 correlation_id 过滤 → 发送给对应客户端
```

### 为什么需要 MessageBus？

Orchestrator 的 `serve()` 是一个长时间运行的循环（一个 session 一个 `asyncio.Task`），而 HTTP 请求处理是短暂的。MessageBus 让它们异步解耦：

1. HTTP handler 将消息放入队列后立即返回（event stream 在后台消费 outbound 队列）
2. `serve()` 持续消费 inbound 队列、执行 agent、将流式输出放入 outbound 队列
3. 多个并发 HTTP 请求通过 `correlation_id` 区分各自的 outbound 消息
4. 同一 session 的多个请求**串行处理**（单队列单消费者），避免并发状态冲突

### InboundMessage / OutboundMessage 数据结构

```python
@dataclass
class InboundMessage:       # core/message_bus.py:51-67
    session_key: str        # 目标会话
    content: str            # 用户输入文本
    source: str             # "cli" | "http" | "websocket" | "telegram"
    correlation_id: str     # UUID hex，用于关联响应
    model: str | None       # 模型覆盖
    temperature: float | None
    max_tokens: int | None
    goal: str | None        # 目标描述（PlanSolve）
    skills: list[str] | None # 显式 skill 列表
    timestamp: float        # time.monotonic()

@dataclass
class OutboundMessage:      # core/message_bus.py:71-96
    session_key: str
    correlation_id: str     # 与 InboundMessage.correlation_id 对应
    msg_type: str           # "delta"|"thinking"|"thinking_done"|"tool_start"
                            # |"tool_end"|"final"|"error"
    data: Any               # 按 msg_type 不同：
                            #   delta/thinking: str (单个 token)
                            #   thinking_done: None
                            #   tool_start: str (工具名称)
                            #   tool_end: dict (name, status, duration_ms, detail)
                            #   final: dict (content, usage, stop_reason, paradigm)
                            #   error: str (错误信息)
    timestamp: float
```

## 缓存策略

| 缓存项 | 位置 | 生命周期 |
|--------|------|----------|
| `index.html` bytes | `create_app()` 闭包 `_ui_html` (line 364) | 首次请求后永久缓存 |
| Static prompt | `ContextManager._static_prompt` | tools 变更时重建 |
| Memory context | `ContextManager._memory_cache` | `remember()`/`forget()` 时失效 |

## 设计要点

- **单文件前端**：零依赖构建，仅靠 marked.js CDN 实现 Markdown 渲染
- **SSE 为主，WebSocket 备用**：SSE 用于单向流式输出，WS 支持双向通信和请求取消
- **MessageBus 解耦**：HTTP/WS 端点与 Orchestrator 通过 asyncio.Queue 完全解耦
- **correlation_id 多路复用**：共享 outbound 队列，每个请求只消费自己的消息
- **按需 serve task**：每个 session 的 `serve()` task 在首次请求时懒启动
- **X-Accel-Buffering: no**：禁用 nginx 代理缓冲，确保 SSE token 实时送达
- **可折叠细节**：Thinking 和 Tool 调用默认折叠，减少视觉噪音
