<!-- Generated: 2026-07-13 | Files scanned: ~100 | Token estimate: ~480 -->

# Data — Storage Layout

## Workspace (`~/.mybot/workspace/` or `$WORKSPACE`)

```
{workspace}/
├── sessions/
│   └── {session_key}.json        # Session messages (JSON array)
├── observability/
│   └── {session_key}.jsonl       # Logs + spans (append-only)
├── memory/
│   ├── SOUL.md                   # Bot identity
│   ├── USER.md                   # User profile
│   ├── MEMORY.md                 # Memory index (pointers)
│   ├── {slug}.md                 # Type-specific memories
│   ├── history.jsonl             # Consolidation summaries
│   └── search.db               # SQLite FTS5 + sqlite-vec index
├── wechat/
│   └── account.json              # iLink bot token + state
├── xiaohongshu/
│   ├── state.json                # {phase, series, last_tangmian_content}
│   ├── {date}_tangmian.md        # Turtle soup puzzle
│   └── {date}_tangdi.md          # Turtle soup answer
├── scheduled_tasks.json           # User-created periodic tasks
└── settings.json                  # First-run auto-generated config
```

## Session Format (`sessions/{key}.json`)
```json
{
  "key": "20260713-xxx",
  "messages": [
    {"role": "user", "content": "...", "timestamp": "2026-07-13T12:00:00"},
    {"role": "assistant", "content": "...", "tool_calls": [...]}
  ],
  "consolidated_cursor": 0,
  "created_at": "2026-07-13T12:00:00",
  "updated_at": "2026-07-13T12:00:00",
  "metadata": {}
}
```

## Observability JSONL Format
```jsonl
{"type":"event","session_key":"s1","event_type":"LLMCallEvent","timestamp":1.0,"data":{...}}
{"type":"span","session_key":"s1","trace_id":"abc","span_id":"s1","name":"agent.run",...}
```

## Memory Files (`memory/types.py`)
| File | Manager | Purpose |
|------|---------|---------|
| `SOUL.md` | MemoryStore / user edit | Bot identity and behavior principles |
| `USER.md` | MemoryStore / user edit | User profile, preferences, tech stack |
| `MEMORY.md` | Dream (LLM merge) | Long-term memory maintained by periodic Dream pipeline |
| `history.jsonl` | Consolidator (LLM summary) | Append-only conversation summaries fed to Dream |
| `memory/{slug}.md` | MemoryStore (remember/forget) | Individual fact memories created via `remember` tool |

## Hybrid Search (`memory/hybrid_store.py`)
- **Vector**: sqlite-vec with sentence-transformers embeddings
- **Full-text**: SQLite FTS5
- **Fusion**: Reciprocal Rank Fusion (RRF) over both indexes
- **Decay**: 30-day half-life exponential decay
