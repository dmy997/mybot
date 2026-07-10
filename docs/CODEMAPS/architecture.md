<!-- Generated: 2026-07-10 | Files scanned: ~90 | Token estimate: ~600 -->

# Architecture

## Entry Points
```
mybot          → core.orchestrator:main           (interactive CLI)
mybot-server   → core.server:main                 (HTTP/WS + Web UI)
mybot-wechat   → channels.wechat:main             (iLink WeChat bot)
```

## System Diagram
```
┌──────────────┐  ┌──────────┐  ┌───────────────┐
│  CLI (Textual)│  │ HTTP/WS   │  │  WeChat iLink  │
│  tui/app.py   │  │ server.py │  │  channels/     │
└──────┬───────┘  └────┬──────┘  │  wechat.py     │
       │               │         └───────┬────────┘
       ▼               ▼                 ▼
┌─────────────────────────────────────────────────┐
│              Orchestrator                        │
│  core/orchestrator.py (~800 lines)               │
│  + background_service.py (cron + scheduled)      │
│  + mcp_service.py (MCP lifecycle)                │
└────────┬───────────────────┬────────────────────┘
         ▼                   ▼
┌─────────────────┐  ┌──────────────────────┐
│  ContextManager  │  │     Dispatcher        │
│  context/        │  │  core/dispatcher.py   │
│  + SessionStore  │  │  4-layer routing:     │
│  + MemoryService │  │  cmd→keyword→LLM→def  │
│  + Compaction    │  └──────────┬───────────┘
│  + TokenBudget   │             ▼
└────────┬────────┘  ┌──────────────────────┐
         ▼           │    Agent Layer        │
   build_messages()  │  ReactAgent           │
         │           │  PlanSolveAgent       │
         ▼           │  DeepResearchAgent    │
┌─────────────────┐  │  + Team (multi-agent) │
│   AgentCore      │  └──────────┬───────────┘
│  core/runner.py  │◄────────────┘
│  LLM loop +      │
│  tool exec +     │
│  compaction +    │
│  error recovery  │
└────────┬─────────┘
         ▼
┌─────────────────┐
│  LLMProvider     │
│  providers/      │
│  OpenAI compat   │
└─────────────────┘
```

## Layer Map

| Layer | Dir | Key Files | Lines |
|-------|-----|-----------|-------|
| Entry | `channels/`, `core/server.py`, `tui/` | wechat.py, server.py | ~2000 |
| Orchestration | `core/` | orchestrator.py, runner.py, dispatcher.py | ~2200 |
| Context | `context/` | context_manager.py, session_store.py, memory_service.py | ~1500 |
| Memory | `memory/` | store.py, hybrid_store.py, consolidator.py, dream.py | ~1200 |
| Agents | `agents/` | react_agent.py, plan_solve_agent.py, deep_research_agent.py | ~800 |
| Providers | `providers/` | openai_compatible_provider.py, base.py | ~1000 |
| Tools | `tools/` | tool.py, guard.py, registry.py + sandbox/ + mcp/ | ~2000 |
| Observability | `observability/` | log.py, metrics.py, trace.py, persistence.py | ~1200 |
| Services | `services/` | cron.py, scheduled_tasks.py | ~600 |
| Config | `config/` | config.py, settings.py | ~300 |

## Data Flow
```
User Input → Channel → MessageBus.inbound(session_key)
  → Orchestrator.serve() → process_message()
    → ContextManager.build_messages() → repair + system prompt + history
    → Dispatcher.resolve() → agent paradigm
    → Agent.run() → AgentCore.run() → LLM ↔ tool loop
    → ContextManager.save_exchange()
    → MessageBus.outbound(source) → Channel → User
```
