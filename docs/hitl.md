# Human-in-the-Loop (HITL)

mybot 支持在关键工具执行前暂停并征求用户确认，防止误操作。

## 模式

| 模式 | 环境变量 | 行为 |
|------|----------|------|
| Confirm | `HITL_MODE=confirm` | SHELL / FILE_WRITE / NETWORK / DELEGATE 需确认（默认） |
| Bypass | `HITL_MODE=bypass` | 所有工具自动执行 |

## 配置

在 `~/.mybot/settings.json` 的 `"env"` 中：

```json
{
  "env": {
    "HITL_MODE": "confirm",
    "HITL_BYPASS_TOOLS": "websearch,webfetch",
    "HITL_TIMEOUT_SECONDS": "120"
  }
}
```

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `HITL_MODE` | `bypass` | 模式: `bypass` / `confirm` |
| `HITL_BYPASS_TOOLS` | `xiaohongshu_publish` | 逗号分隔的工具名，confirm 模式下也跳过 |
| `HITL_TIMEOUT_SECONDS` | `120` | 超时秒数，超时自动拒绝 |

## 架构

```
AgentCore._execute_tool_calls()
  → MiddlewareChain.run_tool_execute()
    → HitlMiddleware.on_tool_execute()
      → HitlService.request_confirmation()  ← asyncio.Future 阻塞
        → 所有注册的 listener 回调
        → Channel 特定 UI 展示确认请求
        → 用户响应 → HitlService.respond()
      → Future 完成 → 允许/拒绝
```

### 核心组件

- **HitlService** (`services/hitl.py`): `asyncio.Future` 确认机制，支持多 listener
- **HitlMiddleware** (`services/hitl.py`): `AgentMiddleware` 子类，在 `on_tool_execute` 中拦截
- 需确认的能力: `SHELL`, `FILE_WRITE`, `NETWORK`, `DELEGATE`
- 只读工具 (`FILE_READ`) 自动放行

## Channel 适配

### CLI (TUI)

使用已有的 `ConfirmScreen` 弹窗展示确认对话框。

### HTTP/Web UI

- SSE `hitl_confirm` 事件推送到浏览器
- `POST /hitl/respond` 端点: `{request_id, decision: "approved"|"denied"}`
- Web UI (`server_web/index.html`) 显示确认对话框

### WeChat

- 发送微信确认消息: "HITL / 工具: {tool_name} / 回复 y 允许，n 拒绝"
- `_on_message` 检测 y/n 回复并调用 `HitlService.respond()`

## API

### `POST /hitl/respond`

```json
// Request
{"request_id": "abc123", "decision": "approved"}

// Response (200)
{"status": "ok", "request_id": "abc123", "decision": "approved"}

// Response (404 - already resolved / not found)
{"error": "request not found or already resolved"}
```

## 超时

- 默认 120 秒，超时自动拒绝（Agent 继续执行，工具调用返回错误）
- 可通过 `HITL_TIMEOUT_SECONDS` 调整
