<!-- Generated: 2026-07-10 | Files scanned: ~90 | Token estimate: ~550 -->

# Backend — API Routes & Middleware

## HTTP Routes (`core/server.py`)

```
GET  /                         → index          (Web UI)
GET  /health                   → health         (liveness)
GET  /metrics                  → metrics        (Prometheus-style)
GET  /logs?limit=&session_key= → logs_endpoint  (structured logs)
GET  /traces?limit=&session_key=→ traces_endpoint(spans)
GET  /observability/sessions   → sessions_obs   (session list)
POST /chat/{session_id}        → chat_sse       (SSE stream)
GET  /events/{session_id}      → push_events    (SSE push channel)
GET  /sessions                 → list_sessions
GET  /sessions/{id}            → get_session
GET  /sessions/{id}/messages   → get_session_messages
DELETE /sessions/{id}          → delete_session
WS   /ws/{session_id}          → ws_endpoint    (bidirectional)
```

## Middleware Chain (`core/middleware.py`)

Chain-of-responsibility, 5 hooks:
```
on_agent_start → on_agent_step (abort OK) → on_llm_call (modify msgs/model)
  → on_tool_execute (block/modify/cache) → on_agent_end
```
Shared state: `MiddlewareContext.data` dict.

## Consumer Model

| Source | Inbound | Outbound | Filter |
|--------|---------|----------|--------|
| CLI | `process_message()` sync | StreamingMessage widget | N/A (single) |
| HTTP SSE | `POST /chat/{sid}` → bus | `outbound("http")` | `correlation_id` |
| WebSocket | ws msg → bus | `outbound("websocket")` | `correlation_id` |
| WeChat | `_on_message()` → bus | `outbound("wechat")` | final only |
| Push | scheduled tasks → bus | `outbound("push:{sid}")` | none |

## Key Service→File Mapping

| Service | File | Init By |
|---------|------|---------|
| `ObservabilityStore` | `observability/persistence.py` | Orchestrator ctor |
| `CronScheduler` | `services/cron.py` | BackgroundService |
| `ScheduledTaskService` | `services/scheduled_tasks.py` | Orchestrator ctor |
| `MCPService` | `core/mcp_service.py` | Orchestrator ctor |
| `SessionStore` | `context/session_store.py` | ContextManager ctor |
| `MemoryService` | `context/memory_service.py` | ContextManager ctor |
| `CompactionService` | `context/compaction.py` | ContextManager ctor |
| `TokenBudget` | `context/token_budget.py` | ContextManager ctor |
| `ToolRegistry` | `tools/registry.py` | Orchestrator ctor |
| `ToolGuard` | `tools/guard.py` | ToolRegistry |
| `Dispatcher` | `core/dispatcher.py` | Orchestrator ctor |
| `EventBus` | `core/events.py` | Orchestrator ctor |
| `SkillsLoader` | `core/skills.py` | ContextManager |
| `Consolidator` | `memory/consolidator.py` | ContextManager |
| `Dream` | `memory/dream.py` | BackgroundService |
