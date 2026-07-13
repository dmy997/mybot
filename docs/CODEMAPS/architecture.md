<!-- Generated: 2026-07-13 | Files scanned: ~100 | Token estimate: ~650 -->

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
│  core/orchestrator.py (~994 lines)               │
│  + background_service.py (cron + scheduled)      │
│  + mcp_service.py (MCP lifecycle)                │
└────────┬───────────────────┬────────────────────┘
         ▼                   ▼
┌─────────────────┐  ┌──────────────────────┐
│  ContextManager  │  │     Dispatcher        │
│  context/        │  │  core/dispatcher.py   │
│  + SessionStore  │  │  4-layer routing:     │
│  + MemoryService │  │  cmd→keyword→LLM→def  │
│  + CompactionService    │  └──────────┬───────────┘
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
| Entry | `channels/`, `core/server.py`, `tui/` | wechat.py, server.py, app.py, widgets.py | ~3228 |
| Channels | `channels/` | base.py, wechat.py | ~1469 |
| Orchestration | `core/` | orchestrator.py, runner.py, dispatcher.py | ~2571 |
| Context | `context/` | context_manager.py, session_store.py, memory_service.py, compaction.py, token_budget.py | ~1501 |
| Memory | `memory/` | store.py, hybrid_store.py, consolidator.py, dream.py | ~1510 |
| Agents | `agents/` | react_agent.py, plan_solve_agent.py, deep_research_agent.py, team/ | ~863 |
| Providers | `providers/` | base.py, openai_compatible_provider.py, errors.py | ~985 |
| Tools | `tools/` | all .py files under tools/ (incl. sandbox/, mcp/) | ~3735 |
| Observability | `observability/` | log.py, metrics.py, trace.py, persistence.py | ~1001 |
| Services | `services/` | cron.py, hitl.py, scheduled_tasks.py, xiaohongshu.py | ~898 |
| Config | `config/` | config.py, settings.py | ~621 |

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
