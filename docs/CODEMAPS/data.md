<!-- Generated: 2026-07-10 | Files scanned: ~90 | Token estimate: ~450 -->

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
│   └── hybrid_store.db           # SQLite FTS5 + sqlite-vec index
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
  "session_key": "20260710-xxx",
  "messages": [
    {"role": "user", "content": "...", "timestamp": 1.0},
    {"role": "assistant", "content": "...", "tool_calls": [...]}
  ],
  "consolidated_cursor": 0,
  "created_at": "2026-07-10T...",
  "last_active": "2026-07-10T..."
}
```

## Observability JSONL Format
```jsonl
{"type":"event","session_key":"s1","event_type":"LLMCallEvent","timestamp":1.0,"data":{...}}
{"type":"span","session_key":"s1","trace_id":"abc","span_id":"s1","name":"agent.run",...}
```

## Memory Types (`memory/types.py`)
| Type | Purpose | Files |
|------|---------|-------|
| `user` | Who the user is, preferences | `user_*.md` |
| `feedback` | How to approach work | `feedback_*.md` |
| `project` | Project context, deadlines | `project_*.md` |
| `reference` | External system pointers | `reference_*.md` |

## Hybrid Search (`memory/hybrid_store.py`)
- **Vector**: sqlite-vec with sentence-transformers embeddings
- **Full-text**: SQLite FTS5
- **Fusion**: Reciprocal Rank Fusion (RRF) over both indexes
- **Decay**: 30-day half-life exponential decay
