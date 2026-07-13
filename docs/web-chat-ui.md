# Web 聊天界面 (Web Chat UI)

## 概述

mybot 提供一个完整的 Web 聊天界面，通过 `mybot-server` 命令启动。后端基于 Starlette（HTTP SSE + WebSocket），前端是单个 HTML 文件（`server_web/index.html`，约 1475 行），使用原生 JavaScript 实现，零构建步骤。页面包含三个可切换视图：聊天、可观测性（Metrics/Log/Trace）。

启动后访问 `http://127.0.0.1:8080` 即可使用。

## 架构总览

```
浏览器 (index.html)
  │
  ├── POST /chat/{session_id}        ───── SSE 流式响应 (chat_sse)
  ├── POST /hitl/respond              ───── HITL 审批响应
  ├── GET  /hitl/pending              ───── 待处理 HITL 请求
  ├── GET  /sessions                  ───── JSON 会话列表
  ├── GET  /sessions/{id}             ───── JSON 单个会话
  ├── GET  /sessions/{id}/messages    ───── JSON 历史消息
  ├── DELETE /sessions/{id}           ───── 删除会话
  ├── GET  /metrics                   ───── Prometheus 风格指标 JSON
  ├── GET  /logs                      ───── 最近结构化日志事件 JSON
  ├── GET  /traces                    ───── 最近 Trace Span JSON
  ├── GET  /observability/sessions    ───── 有可观测性数据的会话列表
  ├── GET  /events/{session_id}       ───── 长连接 SSE 事件推送 (push 源)
  └── WS   /ws/{session_id}           ───── WebSocket 双向通信 (备用)

服务端:
  Starlette app (core/server.py:create_app)
    │
    ├── chat_sse:       POST → InboundMessage → bus_msg.inbound.put()
    │                   → 等待 OutboundMessage → 转为 SSE event → StreamingResponse
    │
    ├── ws_endpoint:    WebSocket → InboundMessage → bus_msg.inbound.put()
    │                   → 等待 OutboundMessage → 转为 JSON → websocket.send_json()
    │
    ├── index:           返回 server_web/index.html (首次读取后缓存为 bytes)
    │
    ├── metrics:         返回 REGISTRY.collect_all() JSON (counters + gauges + histograms)
    │
    ├── logs_endpoint:   返回 recent.get_logs() → 最近 500 条日志
    │
    ├── traces_endpoint: 返回 recent.get_spans() → 最近 200 个 Span
    │
    └── _ensure_serve_task:
        启动 Orchestrator.serve() 后台 Task，按需创建，每 session 一个
```

## 关键模块

| 模块 | 文件 | 职责 |
|------|------|------|
| Server App | `core/server.py:87-445` | Starlette 路由注册、SSE/WS 端点实现 |
| Entry Point | `core/server.py:452-493` | uvicorn 启动、读取 `MYBOT_HOST`/`MYBOT_PORT` 环境变量 |
| Metrics Registry | `observability/metrics.py` | 内存指标注册表 (Counter/Gauge/Histogram)，REGISTRY 单例 |
| Recent Store | `observability/recent.py` | 环形缓冲区，存储最近日志事件和完成 Span |
| MessageBus | `core/message_bus.py` | 解耦 I/O：InboundMessage (入站) + OutboundMessage (出站) |
| Frontend UI | `server_web/index.html` | 单文件 HTML + CSS + JS，marked.js CDN 渲染 Markdown |

## 前端 UI 详解

`server_web/index.html` 是一个完整的单文件聊天应用，包含：

### 布局结构

```
┌──────────────┬──────────────────────────────────────┐
│   Sidebar    │   Main                               │
│   (260px)    │                                      │
│              │   Chat View:                         │
│   Session    │     Chat Header (session name + goal)│
│   List       │     Messages Area                    │
│   (grouped   │       - User bubbles (right, blue)   │
│    by date)  │       - Assistant (left, gray)       │
│              │         - Think blocks (collapsible) │
│     [+ New   │         - Tool blocks (collapsible)  │
│      Session]│       - Usage badges (tokens)        │
│              │     Input Area [text input] [Send]   │
│              │                                      │
│   Sidebar    │   OR Observability View:             │
│   Nav:       │     Metrics panel OR                 │
│    Metrics   │     Log events list OR               │
│    Log       │     Trace waterfall chart            │
│    Trace     │                                      │
└──────────────┴──────────────────────────────────────┘
```

### 视觉特性

- **暗色主题**：CSS 变量定义 15+ 颜色令牌（`--bg`, `--surface`, `--accent`, `--bubble-user`, `--bubble-tool`, `--bubble-think` 等）
- **可折叠区块**：Thinking 和 Tool 调用以折叠块形式展示，点击标题切换展开/折叠，2 秒后 thinking 自动折叠
- **Markdown 渲染**：通过 `marked.js` CDN 将助手回复渲染为 HTML（支持代码块、列表等）
- **加载动画**：CSS 三点跳动动画，流式输出期间显示
- **自适应气泡**：用户消息右对齐（蓝色）、助手消息左对齐（灰色边框）、错误消息居中（红色）
- **Goal 徽章**：通过 `/goal <text>` 命令设置目标，蓝色徽章显示在 header，点击可清除
- **可观测性面板**：侧边栏导航按钮切换 Metrics/Log/Trace 视图，每 5 秒自动刷新
- **消息历史导航**：ArrowUp/ArrowDown 键浏览已发送消息，超出 200 条自动裁剪
- **消息历史持久化**：消息历史通过 `localStorage` 按 session 存储
- **Paradigm Cards**：输入框上方三个可选范式的卡片（Plan / Deep Research / Reflect），点击切换范式，激活时高亮并显示"✓"标记
- **图片附件**：支持从文件选择、剪贴板粘贴、拖拽上传图片，显示缩略图预览并支持移除，随消息以 `images` 数组发送

### 核心 JS 变量

```javascript
currentSession = 'default';     // 当前活跃会话 key
isStreaming = false;            // 流式请求进行中锁
activeObsView = null;           // 活跃可观测性视图 ('metrics'|'logs'|'trace'|null)
obsRefreshTimer = null;         // 可观测性 5s 自动刷新定时器
expandedSpanIds = new Set();    // 已展开的 Span 行 ID 集合
messageHistory = [];            // 已发送消息列表 (for ArrowUp/Down 导航)
historyIndex = -1;              // 当前浏览的历史位置
historyDraft = '';              // 临时保存的未发送输入
currentGoal = null;             // 当前目标文本
```

### 会话管理

```javascript
loadSessions()               // GET /sessions → 按日期分组填充侧边栏
                               // 分组: Today, Yesterday, 星期几, or Month Day, Year
selectSession(key)           // 切换活跃会话 → 加载历史消息 + 切换视图
newSession()                 // prompt() 输入名称 → 创建会话
loadHistory(sessionKey)      // GET /sessions/{key}/messages → 渲染历史消息
                               // 解析 user/assistant/tool 角色，渲染 tool_calls + tool 结果
```

### 聊天流程

```javascript
sendMessage()                // 核心函数 (index.html:853-981)
  ├── addBubble('user', message)                       // 添加用户气泡
  ├── addBubble('assistant', '')                       // 添加助手占位气泡
  ├── 收集 attachedImages (base64 data URLs) → images[]
  ├── fetch('/chat/' + currentSession, { POST })        // 发起 SSE 请求
  │     body: { message, goal: currentGoal, images, paradigm }
  │
  └── reader = resp.body.getReader() 循环解析 SSE 事件
        ├── event: delta          → textBuffer += token → marked.parse() 实时渲染
        ├── event: thinking       → 创建/追加 thinking 折叠块 (自动展开)
        ├── event: thinking_done  → 2 秒后自动折叠 thinking 块
        ├── event: tool_start     → 创建 tool 折叠块 (绿色标题)
        ├── event: tool_end       → 追加 tool 执行结果/错误
        ├── event: hitl_confirm   → 显示 HITL 审批气泡，包含确认/拒绝按钮
        ├── event: done           → 显示 usage 徽章 (in/out tokens)
        └── event: error          → 显示红色错误信息
```

### 键盘快捷键

| 按键 | 操作 |
|------|------|
| Enter | 发送消息 |
| ArrowUp | 上一条消息历史 |
| ArrowDown | 下一条消息历史 |
| /goal <text> + Enter | 设置当前目标（不触发消息发送） |

### SSE 事件流解析

前端手动解析 SSE 流（而非使用 `EventSource`，因为 `EventSource` 不支持 POST）：

```javascript
// index.html:892-971
const reader = resp.body.getReader();
const decoder = new TextDecoder();
let buf = '';

while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });

    // 按 \n\n 分割 SSE 事件
    const parts = buf.split('\n\n');
    buf = parts.pop(); // 保留不完整部分（可能跨 chunk）

    for (const part of parts) {
        // 解析 "event: xxx" 和 "data: {...}" 行
        const lines = part.split('\n');
        let event = '', data = '';
        for (const line of lines) {
            if (line.startsWith('event: ')) event = line.slice(7);
            else if (line.startsWith('data: ')) data = line.slice(6);
        }
        data = JSON.parse(data);

        // 按 event 类型分发到对应处理逻辑
        switch (event) {
            case 'delta':        // 追加 content token → marked.parse()
            case 'thinking':     // 追加 thinking token
            case 'thinking_done':// 2s 后折叠
            case 'tool_start':   // 创建 tool 折叠块
            case 'tool_end':     // 追加 tool 结果
            case 'hitl_confirm': // 显示 HITL 审批气泡
            case 'done':         // 显示 usage 徽章
            case 'error':        // 显示错误信息
        }
    }
}
```

### 图片附件功能

前端支持通过三种方式附加图片到消息中：

**三路输入**：
1. **文件选择**：点击输入框左侧图片按钮（`📷`）弹出文件选择对话框，支持 `image/*` 类型
2. **剪贴板粘贴**：监听 `paste` 事件，检测 `ClipboardEvent.clipboardData` 中的 `image/png` 或 `image/jpeg` 类型的 `File` 对象，自动添加到附件列表
3. **拖拽上传**：监听 `dragover`/`dragleave`/`drop` 事件，拖拽时输入框区域显示虚线边框（`.drop-active`），放下后提取 `DataTransfer.files` 中的图片文件

**处理流程**：
- 每个图片文件通过 `_addImageFromFile()` 函数处理：
  1. 使用 `FileReader.readAsDataURL()` 转为 base64 data URL
  2. 创建缩略图预览元素（`.image-preview-item`），包含 `<img>` 和移除按钮（`.remove-btn`）
  3. 将 data URL 加入 `attachedImages` 数组

**发送**：
- `sendMessage()` 在 POST body 中携带 `images: attachedImages` 数组（base64 data URL 列表）
- 服务器端 `chat_sse()` 端点解析 `images` 字段，传递给 `process_message()`
- 输入框 placeholder 提示：`"Type a message... (paste/drop images)"`

### Paradigm Cards（范式选择卡）

输入框上方显示三个可选范式卡片：
```html
<div id="paradigm-cards">
  <div class="paradigm-card" data-paradigm="plan_solve">/plan</div>
  <div class="paradigm-card" data-paradigm="deep_research">/research</div>
  <div class="paradigm-card reflect-card" data-paradigm="reflect">Reflect</div>
</div>
```

- 点击卡片切换对应范式，激活的卡片高亮并显示 "✓" 前缀
- Reflect 卡片使用虚线边框（`.reflect-card`），激活时变为实线
- `sendMessage()` 在 POST body 中携带 `paradigm` 字段覆盖 Dispatcher 路由
- 各范式对应的 `/plan`、`/research` 标签映射从 `labels` 对象读取（`index.html:1108`）

### 可观测性视图

侧边栏底部包含三个导航按钮，用于切换到可观测性面板：

**Metrics（`/metrics` 端点）** (`index.html:563-615`)：
- LLM 全局指标：调用次数、错误数、总 token 数、延迟 p50/p95/avg/max
- Tools 全局指标：调用次数、错误数、延迟 p50/p95/avg/max
- Agent 全局指标：平均步数、最大步数、错误数、stall 警告、活跃会话数
- 非 default session 时顶部显示 session 标识卡片

**Log（`/logs` 端点）** (`index.html:620-657`)：
- 最近 100 条结构化日志事件（支持 `?limit=N` 查询参数，服务端上限 500）
- 每条日志显示：相对时间、event_type、key=value 数据对
- 非 default session 时自动按 `session_key` 过滤

**Trace（`/traces` 端点）** (`index.html:662-808`)：
- 最近 100 个 Trace，按结束时间降序排列
- Jaeger 风格瀑布图展示各个 span 的耗时和层级关系
- Span 类型颜色编码：llm（紫色）、tool（绿色）、orchestrator（橙色）、agent（淡紫）
- 可展开查看 Attributes / Input / Output 详情
- 展开状态在刷新时保持（通过 `expandedSpanIds` Set）
- 非 default session 时自动过滤根 span 匹配的 trace

## 服务端详解

### 启动流程

```
mybot-server 命令
  └── core/server.py:main()  (line 452-493)
        ├── 读取环境变量 MYBOT_HOST (默认 127.0.0.1), MYBOT_PORT (默认 8080)
        ├── 创建 OpenAICompatibleProvider (读取 API key/base/name/model)
        ├── 创建 Orchestrator(workspace, provider, compress_model)
        │     └── Orchestrator.__init__()  → 创建 ContextManager, Dispatcher,
        │         ToolRegistry, CronScheduler, Dream
        │
        ├── create_app(orchestrator) → Starlette app  (line 87-445)
        │     └── 注册路由 (见下方路由表)
        │
        ├── orchestrator.start_services()  # 启动 MCP + Cron
        └── uvicorn.Server(app).serve()
```

### 路由表

`core/server.py:431-443`

| 方法 | 路径 | 处理函数 | 说明 |
|------|------|----------|------|
| `GET` | `/` | `index()` (line 418) | 返回 `server_web/index.html`（首次读取后缓存为 bytes） |
| `GET` | `/health` | `health()` (line 137) | 健康检查，返回 `{"status": "ok"}` |
| `GET` | `/metrics` | `metrics()` (line 140) | 返回 REGISTRY.collect_all() → `{counters, gauges, histograms}` |
| `GET` | `/logs` | `logs_endpoint()` (line 149) | 返回最近结构化日志事件 JSON（支持 `?limit=N`） |
| `GET` | `/traces` | `traces_endpoint()` (line 154) | 返回最近 Trace Span JSON（支持 `?limit=N`） |
| `POST` | `/chat/{session_id}` | `chat_sse()` (line 159) | SSE 流式聊天：发送消息 → 接收流式响应 |
| `GET` | `/sessions` | `list_sessions()` (line 248) | 列出所有会话（JSON 数组，含 `key`/`message_count`/`updated_at`） |
| `GET` | `/sessions/{session_id}` | `get_session()` (line 254) | 获取单个会话详情 |
| `GET` | `/sessions/{session_id}/messages` | `get_session_messages()` (line 264) | 获取会话历史消息（含 tool_calls、tool 结果） |
| `DELETE` | `/sessions/{session_id}` | `delete_session()` (line 271) | 删除会话 |
| `WS` | `/ws/{session_id}` | `ws_endpoint()` (line 284) | WebSocket 双向通信（备用通道，支持取消） |

### 认证机制

`core/server.py:58-79`

```
_check_auth(request):
  MYBOT_API_KEY 为空 → 允许所有请求 (auth disabled)
  MYBOT_API_KEY 设置 → 校验 Authorization: Bearer <key>

_ws_check_auth(headers):
  同上，从 WebSocket headers 中提取 Authorization
```

所有路由 except `/` 和 `/health` 均受认证保护。未认证返回 `{"error": "unauthorized"}` + 401。

### 请求处理完整调用链

```
1. 用户在前端输入消息，点击 Send

2. 前端: fetch POST /chat/{session_id}  (index.html:880-884)
     body: {"message": "你好", "goal": null}

3. Server: chat_sse()  (core/server.py:159-246)
     │
     ├── _check_auth(request)  → 校验 Bearer token
     │
     ├── 解析 request body → message, model, temperature, goal
     │   生成 correlation_id = uuid4().hex  (line 176)
     │
     ├── _ensure_serve_task(session_id)  (line 118-131)
     │   # 首次请求时创建 Orchestrator.serve() 后台 asyncio.Task
     │   # serve() 是长时间运行的循环，持续从 inbound 队列读消息
     │   # 每个 session 最多一个 serve task（懒启动）
     │   # 使用 asyncio.Lock 防止 TOCTOU 竞争
     │
     ├── bus_msg.inbound(session_id).put(InboundMessage(
     │       session_key=session_id,
     │       content=message,
     │       source="http",
     │       correlation_id=cid,
     │       model=model,
     │       temperature=temperature,
     │       goal=goal,
     │   ))                                    (line 181-189)
     │
     └── async def event_stream():  ← 返回为 StreamingResponse
           │                              (line 178-236)
           │  Orchestrator.serve() 从 inbound 队列取出消息 → 处理
           │  → 调用 process_message() → 执行 agent → 产生流式输出
           │  → 每个流式事件作为 OutboundMessage 放入 outbound 队列
           │
           └── while True:                    (line 192-234)
                 out = await bus_msg.outbound.get()
                 if out is None: break
                 if out.correlation_id != cid: continue  # 过滤非本请求
                 │
                 ├── msg_type == "delta"          → event: delta
                 ├── msg_type == "thinking"       → event: thinking
                 ├── msg_type == "thinking_done"  → event: thinking_done
                 ├── msg_type == "tool_start"     → event: tool_start
                 ├── msg_type == "tool_end"       → event: tool_end
                 ├── msg_type == "tool_exec_start"→ event: tool_exec_start
                 ├── msg_type == "tool_exec_end"  → event: tool_exec_end
                 ├── msg_type == "final"          → event: done (含 metrics) → break
                 └── msg_type == "error"          → event: error → break

4. 前端: 解析 SSE 事件流 → 逐步渲染到页面  (index.html:896-971)
     │
     ├── delta:       textBuffer += token → marked.parse() → DOM
     ├── thinking:    创建/追加 think-block → textContent
     ├── tool_start:  创建 tool-block → 追加到 assistant 气泡
     ├── tool_end:    追加结果/错误到 tool-body
     ├── done:        显示 usage 徽章 + 刷新 sessions + 刷新 obs
     └── error:       显示红色错误信息
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
        ├── 根据 InboundMessage 构造回调:
        │     _on_delta(token)                → OutboundMessage("delta")
        │     _on_thinking(token)             → OutboundMessage("thinking")
        │     _on_thinking_done()             → OutboundMessage("thinking_done")
        │     _on_tool_start(name, args)      → OutboundMessage("tool_start")
        │     _on_tool_end(name, status, ...) → OutboundMessage("tool_end")
        │     _on_tool_exec_start(...)        → OutboundMessage("tool_exec_start")
        │     _on_tool_exec_end(...)          → OutboundMessage("tool_exec_end")
        │
        ├── result = await self.process_message(
        │       session_key, user_input, model, temperature, goal,
        │       on_delta=_on_delta,
        │       on_thinking=_on_thinking,
        │       on_tool_start=_on_tool_start,
        │       on_tool_execute_start=_on_tool_exec_start,
        │       on_tool_execute_end=_on_tool_exec_end,
        │   )
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

### `done` 事件中的 metrics 数据

`core/server.py:219-230`

`done` 事件除了 `content`/`usage`/`stop_reason`/`paradigm` 外，还携带当前指标快照：

```python
{
    "content": "...",
    "stop_reason": "completed",
    "paradigm": "react",
    "usage": {"prompt_tokens": 123, "completion_tokens": 456},
    "metrics": {
        "counters":   {"llm_calls_total": 5, "llm_tokens_total": 9000, ...},
        "gauges":     {"active_sessions": 2},
        "histograms": {"llm_latency_ms": {"p50": 800, "p95": 2200, "avg": 1100, "max": 3500},
                       "tool_latency_ms": {...}, "agent_steps": {...}},
    }
}
```

前端目前仅提取 `usage` 显示 token 徽章，metrics 数据通过 `/metrics` 端点单独获取。

### SSE 事件格式

`core/server.py:43-50`

```python
def _sse_event(event: str, data=None) -> str:
    """格式化 SSE 事件字符串。

    输出格式:
      event: delta\n
      data: {"token": "你好"}\n
      \n
    """
    lines = [f"event: {event}"]
    if data is not None:
        payload = json.dumps(data, ensure_ascii=False)
        lines.append(f"data: {payload}")
    lines.append("")
    return "\n".join(lines) + "\n"
```

响应头设置 (`core/server.py:238-246`)：
- `Content-Type: text/event-stream` — SSE 媒体类型
- `Cache-Control: no-cache` — 禁用浏览器缓存
- `Connection: keep-alive` — 保持 TCP 连接
- `X-Accel-Buffering: no` — 禁用 nginx 代理缓冲（确保 token 实时送达）

### WebSocket 端点

`core/server.py:284-410`

WebSocket 端点（`/ws/{session_id}`）提供与 SSE 端点等价的功能，使用双向 JSON 消息，额外支持请求取消：

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
  {"type": "tool_exec_start", "name": "bash", "args": {...}, ...}
  {"type": "tool_exec_end", "name": "bash", "status": "ok", ...}
  {"type": "new_turn"}
  {"type": "done", "content": "...", "usage": {...}, "metrics": {...}}
  {"type": "error", "message": "..."}
```

WebSocket 支持**请求取消**：
- 新的 `chat` 消息会取消当前正在进行的 task（`_current_task.cancel()`）
- `cancel` 消息类型明确请求取消
- WebSocket 断开时自动取消进行中的 task

## JavaScript 架构详解

### 1. SSE 流式读取模式

前端使用 `fetch()` + `Response.body.getReader()` 手动读取 SSE 流，而非 `EventSource` API，因为需要：
- POST 方法发送消息体（含 goal、model 等参数）
- 支持自定义请求头（如 Authorization）
- 更精细的错误处理

读取循环按 `\n\n` 分割事件块，留存最后一个不完整块跨下次读取拼接：

```javascript
const reader = resp.body.getReader();
const decoder = new TextDecoder();
let buf = '';
while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const parts = buf.split('\n\n');
    buf = parts.pop();  // 保留跨 chunk 的不完整部分
    for (const part of parts) { /* 解析 event + data 行 */ }
}
```

### 2. Goal 命令处理

`currentGoal` 变量和 `setGoal()` 函数 (`index.html:334,514-522`):

```
用户输入 "/goal 实现登录功能" + Enter
  → handleInputKey() 检测以 /goal 开头
  → setGoal("实现登录功能")
      → 更新 currentGoal 变量
      → 显示蓝色 "Goal: 实现登录功能" 徽章
      → 后续 sendMessage() 自动在 POST body 中携带 goal
  → 点击徽章 → setGoal(null) 清除
```

### 3. 消息渲染管线

```
SSE delta 事件
  → textBuffer += token
  → contentEl.innerHTML = marked.parse(textBuffer)
  → scrollDown()

SSE thinking 事件
  → 创建 .think-block (如不存在)
  → thinkingBody.textContent += token
  → scrollDown()

SSE tool_start/tool_end 事件
  → 创建 .tool-block (按 name 去重)
  → toolBody.textContent += result/detail
  → 状态错误时标题变红
```

各渲染节点：
- **assistant 气泡** (`<div class="message assistant">`)：内含 `.msg-content` 渲染 Markdown，以及零或多个 `.think-block` / `.tool-block`
- **think-block**：灰色背景折叠块，默认打开，`thinking_done` 后 2 秒自动折叠
- **tool-block**：绿色标题折叠块，默认打开，展示工具名称和结果

### 4. 历史消息加载

`GET /sessions/{session_id}/messages` 返回完整消息数组：
- `role: "user"` → 直接渲染为 `.message.user` 气泡
- `role: "assistant"` → 渲染 content 部分 + tool_calls 数组（每个调用渲染为独立 tool-block）
- `role: "tool"` → 根据 `tool_call_id` 映射到对应 tool-block 的结果区域

### 5. Metrics 收集与展示

- 每个 SSE `done` 事件携带 `metrics` 数据（含 counters/gauges/histograms 快照）
- 独立 `/metrics` 端点提供汇总指标，前端通过 `showMetrics()` 每 5 秒刷新
- Metrics 视图展示三个卡片：LLM、Tools、Agent，含 p50/p95/avg/max 延迟

### 6. 会话管理

- **列表**：`loadSessions()` 按 `updated_at` 降序排列，按 Today/Yesterday/Weekday/Date 分组
- **创建**：`newSession()` 通过 `prompt()` 输入名称
- **切换**：`selectSession(key)` 切换激活状态，加载历史消息
- **删除**：确认对话框后 `DELETE /sessions/{key}`，如删除当前会话则回退到 default

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
    msg_type: str           # "delta"|"thinking"|"thinking_done"
                            # |"tool_start"|"tool_end"|"tool_exec_start"|"tool_exec_end"
                            # |"final"|"error"
    data: Any               # 按 msg_type 不同：
                            #   delta/thinking: str (单个 token)
                            #   thinking_done: None
                            #   tool_start: str (工具名称)
                            #   tool_end: dict (name, status, duration_ms, detail)
                            #   tool_exec_start: dict (name, args, ...)
                            #   tool_exec_end: dict (name, status, duration_ms, ...)
                            #   final: dict (content, usage, stop_reason, paradigm)
                            #   error: str (错误信息)
    timestamp: float
```

## 缓存策略

| 缓存项 | 位置 | 生命周期 |
|--------|------|----------|
| `index.html` bytes | `create_app()` 闭包 `_ui_html` (line 416) | 首次请求后永久缓存 |
| Static prompt | `ContextManager._static_prompt` | tools 变更时重建 |
| Memory context | `ContextManager._memory_cache` | `remember()`/`forget()` 时失效 |
| 消息历史 | `localStorage` (前端 `_historyKey`) | 按 session 持久化，最多 200 条 |

## 设计要点

- **单文件前端**：零依赖构建，仅靠 marked.js CDN 实现 Markdown 渲染
- **SSE 为主，WebSocket 备用**：SSE 用于单向流式输出，WS 支持双向通信和请求取消
- **MessageBus 解耦**：HTTP/WS 端点与 Orchestrator 通过 asyncio.Queue 完全解耦
- **correlation_id 多路复用**：共享 outbound 队列，每个请求只消费自己的消息
- **按需 serve task**：每个 session 的 `serve()` task 在首次请求时懒启动，`asyncio.Lock` 防竞争
- **X-Accel-Buffering: no**：禁用 nginx 代理缓冲，确保 SSE token 实时送达
- **可折叠细节**：Thinking 和 Tool 调用默认折叠，减少视觉噪音
- **三合一视图**：同一页面集成 Chat + Metrics + Log + Trace，侧边栏导航切换
- **内建可观测性**：无需外部监控工具，通过 `/metrics`/`/logs`/`/traces` 端点暴露全量运行时数据
