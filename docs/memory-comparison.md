# 记忆系统对比分析

nanobot、Claude Code、OpenClaw、mybot 四个项目的记忆系统，从存储、写入触发、召回到上下文三个维度对比。

## 一、文件系统存储

### nanobot

```
workspace/
├── SOUL.md                      # bot 人格/语气
├── USER.md                      # 用户画像
├── HEARTBEAT.md                 # 周期性任务清单
└── memory/
    ├── MEMORY.md                # 长期记忆（Dream 维护）
    ├── history.jsonl            # 追加式对话摘要（Consolidator 写入）
    ├── .cursor                  # Consolidator 写入游标
    ├── .dream_cursor            # Dream 消费游标
    ├── skills/<name>/SKILL.md   # 可复用工作流模式（Dream 创建）
    └── .consolidate-lock        # 进程间互斥锁
```

- **MEMORY.md**：Dream 两阶段维护。Phase 1 LLM 分析产出 `[FILE]`/`[FILE-REMOVE]`/`[SKILL]` 指令，Phase 2 AgentRunner 用文件工具精确编辑。
- **history.jsonl**：JSONL 追加格式。`{"cursor": N, "timestamp": "...", "content": "..."}`。全局共享，不区分 session/channel。
- **skills/**：Dream 发现重复出现的工作流时创建 SKILL.md，作为可复用指令集。
- **行龄注释**：MEMORY.md 中每行可带 `← Nd` 后缀标记距上次修改的天数，辅助去重判断。

### Claude Code

```
~/.claude/projects/<sanitized-git-root>/memory/
├── MEMORY.md                    # 索引文件（所有条目的目录）
├── user_role.md                 # 用户角色/偏好
├── feedback_testing.md          # 反馈记忆
├── project_initiative.md        # 项目记忆
├── reference_slack.md           # 外部系统引用
├── .consolidate-lock            # 进程间互斥（mtime = 上次合并时间）
├── logs/YYYY/MM/YYYY-MM-DD.md   # KAIROS 模式每日日志
└── team/                        # 团队记忆（feature-gated）
    └── MEMORY.md

~/.claude/
├── CLAUDE.md                    # 用户私有全局指令
└── rules/*.md                   # 用户全局规则

<project>/
├── CLAUDE.md                    # 项目指令（可提交到仓库）
├── .claude/CLAUDE.md            # 备选项目指令位置
├── .claude/rules/*.md           # 项目规则（支持 paths: 条件匹配）
└── CLAUDE.local.md              # 私有项目指令（不可提交）
```

- **MEMORY.md**：索引文件，每行 `- [Title](file.md) — one-line hook`。限制 200 行 / 25KB。
- **个体 .md 文件**：YAML frontmatter（`name`, `description`, `metadata.type`）+ Markdown 正文。四种类型：`user`, `feedback`, `project`, `reference`。
- **CLAUDE.md 系列**：五层指令体系（Managed/User/Project/Local/AutoMem），通过 `@include` 指令嵌套引用，深度上限 5。
- **Session Memory**：独立系统，存储在 `~/.claude/session-memory/YYYY/MM/<id>.md`，结构化笔记（Current State, Workflow, Errors 等分段），每段 2000 token 上限，总计 12000 token。仅当前会话有效。

### OpenClaw

```
~/.openclaw/workspace/
├── MEMORY.md                    # 权威长期记忆
├── memory/YYYY-MM-DD.md         # 每日运行笔记
├── memory/YYYY-MM-DD-<slug>.md  # 命名日笔记（/new, /reset）
├── DREAMS.md                    # Dream 日记（人类可读合并报告）
├── SOUL.md                      # 人格/声音
├── IDENTITY.md                  # 名称/vibe/emoji
├── USER.md                      # 用户档案
├── AGENTS.md                    # 指令和优先级
├── TOOLS.md                     # 工具使用约定
├── HEARTBEAT.md                 # 心跳任务
├── BOOTSTRAP.md                 # 启动文件
└── memory/.dreams/              # Dream 内部状态
    ├── short-term-recall.json   # 短期召回评分存储
    ├── phase-signals.json       # 阶段增强信号
    ├── daily-ingestion.json     # 日记摄入状态
    ├── session-ingestion.json   # 会话摄入状态
    ├── session-corpus/          # 脱敏会话片段
    └── events.jsonl             # 事件日志

~/.openclaw/memory/<agentId>.sqlite  # 搜索索引（SQLite + sqlite-vec）
```

- **SQLite 数据库**：包含 `files`, `chunks`（文本块 + embedding + 行号）, `embedding_cache`（按 provider/model 缓存向量）, `meta`, **FTS5 全文索引**（BM25 评分，支持 `trigram` 分词器处理 CJK）。
- **MEMORY.md**：人读 + 机器读。启动时注入上下文，超预算时截断但磁盘上保持完整。
- **DREAMS.md**：Dream 的三个阶段（Light/Deep/REM）的日记输出，供人类审查。
- **memory/YYYY-MM-DD.md**：日记文件。今天 + 昨天的自动加载。memory flush 的写入目标。

### mybot

```
workspace/
├── SOUL.md                      # bot 身份/行为准则
├── USER.md                      # 用户画像
├── memory/
│   ├── MEMORY.md                # 长期记忆（Dream 维护）
│   ├── history.jsonl            # 追加式对话摘要（Consolidator 写入）
│   ├── .cursor                  # Consolidator 写入游标
│   ├── .dream_cursor            # Dream 消费游标
├── cron/
│   └── cron_state.json          # Cron 调度器状态
└── sessions/                    # 会话 JSON 文件

prompt_templates/
├── AGENTS.md                    # 静态指令（非记忆文件）
└── HEARTBEAT.md                 # 心跳任务模板（非记忆文件）
```

- **history.jsonl**：追加格式。`{"cursor": N, "timestamp": "...", "content": "..."}`。全局共享。
- **MEMORY.md**：LLM 直接产出的 Markdown。Dream 单次调用更新全部内容。
- **旧格式条目**（已移除）：`memory/{user,feedback,project,reference}/*.md` YAML frontmatter 格式已被移除，所有记忆统一存储在 MEMORY.md 中。旧目录的遗留文件不再被任何代码读取。
- **AGENTS.md / HEARTBEAT.md**：在 `prompt_templates/` 下，是静态模板，不受 Dream 管理。与 Claude Code/OpenClaw 不同，它们不在工作区目录下。

### 存储维度小结

| 维度 | nanobot | Claude Code | OpenClaw | mybot |
|------|---------|-------------|----------|-------|
| 核心记忆文件 | MEMORY.md（单文件） | 索引 MEMORY.md + 个体 .md | MEMORY.md（单文件） | MEMORY.md（单文件） |
| 对话摘要存储 | history.jsonl（JSONL 追加） | 无（直接写个体 .md） | SQLite chunk（embedding + FTS） | history.jsonl（JSONL 追加） |
| 全文搜索 | 无 | 无（Sonnet sideQuery 选文件） | SQLite FTS5 + sqlite-vec 混合 | 无 |
| 用户档案 | USER.md | USER.md（个体文件） | USER.md | USER.md |
| Bot 人格 | SOUL.md | 无（CLAUDE.md 指令替代） | SOUL.md + IDENTITY.md | SOUL.md |
| 会话笔记 | SessionMemory（结构化分段） | Session Memory（分段，12000 token） | 无独立系统 | Session（JSON 持久化） |

---

## 二、记忆写入触发策略

### nanobot

| 触发机制 | 触发时机 | 频率 | 写入目标 |
|---------|---------|------|---------|
| **Consolidator** | Token 预算检查（超过 consolidation_ratio） | 每次对话后（fire-and-forget） | history.jsonl |
| **Dream** | CronService `register_system_job` | 每 2 小时（可配） | MEMORY.md, USER.md, SOUL.md, skills/ |
| **HeartbeatService** | 独立 asyncio 定时循环 | 每 30 分钟 | 执行 HEARTBEAT.md 中的任务 |
| **AutoCompact** | 消息循环 1 秒超时内检查 session_ttl | 空闲会话过期时 | 压缩后持久化 |

**关键特性**：
- 三层压缩金字塔（micro_compact 规则 → auto_compact LLM → full_compact 用户触发）
- `register_system_job` 幂等——重启后同名 job 覆盖，不会重复注册
- Consolidator 异步 fire-and-forget，per-session asyncio.Lock 防并发
- CronService 自驱动 `_arm_timer` 闭环，不依赖用户输入

### Claude Code

| 触发机制 | 触发时机 | 频率 | 写入目标 |
|---------|---------|------|---------|
| **主 Agent 直接写入** | LLM 自主决定（系统提示词指令） | 对话期间任何时刻 | 个体 .md 文件 + MEMORY.md 索引 |
| **Background Extraction Agent** | `handleStopHooks`（每轮对话结束后） | 每轮（可节流，默认每轮） | 个体 .md 文件 + MEMORY.md 索引 |
| **Auto-Dream Consolidation** | `handleStopHooks`（三重门控检查） | ≥24h + ≥5 会话 + lock | 合并/去重/更新 MEMORY.md + 个体文件 |
| **Session Memory** | Post-sampling hook | 10000 token 首次，5000 token 增量，3 tool call 间隔 | `~/.claude/session-memory/` |

**关键特性**：
- **主 Agent 与 Extraction Agent 互斥**：如果主 Agent 已写记忆，Extraction 跳过（advance cursor 但不执行）
- **三重 Dream 门控**：时间闸（≥24h）→ 会话数闸（≥5 sessions）→ 锁闸（PID fencing，1h stale）。门控廉价优先（cheapest-first check）
- **SideQuery 选文件**：Sonnet 小模型独立调用选择相关记忆文件（最多 5 个），支持 relevancy → staleness → overlap 排序
- **Prompt 四阶段合并**：Orient（读现有记忆）→ Gather（扫描近期信号）→ Consolidate（写入/合并）→ Prune（去重/更新索引）
- **节流**：Extraction 频率和 Dream 频率均可配置

### OpenClaw

| 触发机制 | 触发时机 | 频率 | 写入目标 |
|---------|---------|------|---------|
| **Agent 直接写入** | 用户显式要求 + LLM 自主决定 | 对话期间 | MEMORY.md / memory/YYYY-MM-DD.md |
| **Memory Flush（压缩前）** | 上下文预算低于 `softThresholdTokens` | 压缩触发前（静默回合） | memory/YYYY-MM-DD.md |
| **Dreaming Light Phase** | 内置 cron 调度 | 每 6 小时 | 摄入信号到 short-term-recall.json |
| **Dreaming Deep Phase** | 内置 cron 调度 | 每天 3am | 晋级条目到 MEMORY.md |
| **Dreaming REM Phase** | 内置 cron 调度 | 每周 | 提取模式到 phase-signals.json |
| **短期晋级** | 后台异步 | 按需 | 评分超过 minScore → MEMORY.md |
| **Compaction Checkpoints** | 预算/溢出/手动 | 会话内 | 压缩点后的会话摘要 |

**关键特性**：
- **内置 Cron 托管**：Dream 的三个阶段用 cron 表达式直接调度（`[managed-by=memory-core.short-term-promotion]` tag）
- **加权评分晋级**：频率 0.24 + 相关性 0.30 + 查询多样性 0.15 + 时效 0.15 + 合并度 0.10 + 概念丰富度 0.06。每项有下限闸（minScore=0.8, minRecallCount=3, minUniqueQueries=3）
- **静默回合（Memory Flush）**：压缩前给 Agent 一次不说话的机会将重要上下文写回 memory/
- **Embedding 缓存**：按 (provider, model, hash) 缓存向量，避免重复计算
- **Dreaming 是 opt-in**：默认关闭，需显式配置 `dreaming.enabled: true`

### mybot

| 触发机制 | 触发时机 | 频率 | 写入目标 |
|---------|---------|------|---------|
| **Consolidator** | `process_message()` 中 token 预算检查 | 每次对话后（fire-and-forget） | history.jsonl |
| **Dream** | CronScheduler 后台 timer 触发 | 每 2 小时 | SOUL.md, USER.md, MEMORY.md |
| ContextManager.remember() | 工具调用 / 用户命令 | 手动触发 | MEMORY.md（与 Dream 共享） |

**关键特性**：
- **参考 nanobot 架构**：Consolidator（实时）+ Dream（周期），简化了 CronService → CronScheduler
- **Dream 两阶段**：Phase 1 LLM → `[FILE]`/`[FILE-REMOVE]` 指令，Phase 2 程序化解折并应用到三份文件
- **CronScheduler 自驱动**：`_arm_timer` 闭环（同 nanobot），不依赖用户输入
- **缺项**：无 HeartbeatService、无 LLM-based AutoCompact、无旧 session 过期机制

### 写入触发维度小结

| 维度 | nanobot | Claude Code | OpenClaw | mybot |
|------|---------|-------------|----------|-------|
| **实时摘要写入** | Consolidator（token 预算） | Extraction Agent（每轮） + Session Memory | Memory Flush（压缩前静默回合） | Consolidator（token 预算） |
| **周期合并** | Dream（每 2h，cron job） | Auto-Dream（≥24h + ≥5 sessions） | Dreaming Light/Deep/REM（cron 调度） | Dream（每 2h，CronScheduler） |
| **LLM 写入方式** | 两阶段：分析指令 → AgentRunner 文件编辑 | 四阶段 Prompt：Orient → Gather → Consolidate → Prune | 加权评分晋级 | 两阶段：分析指令 → 程序化合并 |
| **写入粒度** | 文件级（[FILE]/[FILE-REMOVE] 指令） | 文件级（个体 .md 文件 + 索引更新） | Chunk 级（SQLite embedding + FTS） | 文件级（[FILE]/[FILE-REMOVE] 指令） |
| **节流控制** | 有（token 预算） | 有（时间 + 会话数 + lock 三重闸） | 有（评分闸 + cron 调度） | 有（token 预算，固定 2h） |
| **并发保护** | per-session Lock + filelock | PID fencing + .consolidate-lock | 内置 cron 托管，单进程 | per-session Lock |

---

## 三、记忆召回到上下文策略

### nanobot

| 召回路径 | 触发时机 | 召回内容 | 限制 |
|---------|---------|---------|------|
| **System Prompt 注入** | 每次 `build_messages()` | SOUL.md + USER.md + MEMORY.md 全文 | 无明确限制 |
| **history.jsonl 注入** | Dream 游标后的未处理条目 | "Recent History" 段落 | 最近 N 条 |
| **压缩摘要注入** | `read_history_summaries()` | auto_compact 产出的会话级摘要 | 最近 10 条，每条 2000 字符 |

**特点**：
- **始终注入**：SOUL.md, USER.md, MEMORY.md 全文始终在系统提示词中
- **过渡层**：history.jsonl 中 Dream 尚未处理的条目作为 "Recent History" 注入
- **无检索/过滤**：不按查询相关性筛选记忆，全部注入

### Claude Code

| 召回路径 | 触发时机 | 召回内容 | 限制 |
|---------|---------|---------|------|
| **System Prompt 注入** | Session 启动时（缓存整个 session） | MEMORY.md 索引文件全文 + 记忆系统使用说明 | 200 行 / 25KB |
| **Relevant Memory Prefetch** | 每轮 `queryLoop()` 开始时（feature-gated） | Sonnet sideQuery 评选的最相关 ≤5 个 .md 文件 | 每文件 200 行 / 4KB，每 session 累计 60KB |
| **CLAUDE.md 注入** | `prependUserContext()` 每轮 API 调用前 | 五层 CLAUDE.md（Managed/User/Project/Local/AutoMem） | 无明确限制 |
| **Nested Memory** | 读取文件时触发 | 目标路径到 CWD 之间的 `.claude/rules/*.md` | `paths:` 条件匹配 |

**特点**：
- **智能选择**：Sonnet sideQuery 根据用户查询语义选择 ≤5 个最相关记忆文件（relevancy → staleness → overlap 排序）
- **去重**：检查已在消息中展示过的记忆（`collectSurfacedMemories`）避免重复注入
- **缓存策略**：System Prompt 中 MEMORY.md 在整个 session 中不变（不重新加载）
- **When AutoMem enabled, filter from CLAUDE.md**：`filterInjectedMemoryFiles()` 从 CLAUDE.md 注入中移除 AutoMem/TeamMem（prefetch 已处理）
- **SideQuery 并行**：相关记忆 prefetch 是非阻塞的——未就绪时跳过，下一轮重试

### OpenClaw

| 召回路径 | 触发时机 | 召回内容 | 限制 |
|---------|---------|---------|------|
| **Bootstrap 文件注入** | 每次 DM session 启动 | MEMORY.md + SOUL.md + USER.md + AGENTS.md + 今天/昨天日记 | 超预算时 MEMORY.md 截断 |
| **Active Memory（子 Agent）** | 每次回复前（阻塞） | `<active_memory_plugin>` 包裹的相关记忆 | 子 Agent 搜索结果 |
| **memory_search 工具** | Agent 自主调用 | 混合搜索（向量 + BM25 FTS）+ MMR 重排 | 可配 lambda (0.7), top-k |
| **memory_get 工具** | Agent 自主调用 | 按路径直接读取记忆文件 | 无 |

**特点**：
- **混合搜索**：向量语义搜索 + BM25 关键词搜索，可配置权重
- **时间衰减**：指数衰减，默认半衰期 30 天。MEMORY.md 和非日期路径永久保留（不衰减）
- **MMR 多样性重排**：lambda=0.7，Jaccard 相似度（token 化，支持 CJK）
- **跨语料搜索**：memory + wiki + session transcripts
- **Active Memory 阻塞模式**：主回复前必须完成子 Agent 搜索，确保记忆可用但不适合流式场景
- **Chunk 级召回**：memory_search 返回的是 chunk（文本块），不是完整文件

### mybot

| 召回路径 | 触发时机 | 召回内容 | 限制 |
|---------|---------|---------|------|
| **System Prompt 注入** | 每次 `build_messages()` | SOUL.md + USER.md + MEMORY.md 全文（通过 `get_memory_context()`） | 模板内容被过滤 |
| **Recent History 注入** | 每次 `build_messages()` | Dream 游标后的 history.jsonl 未处理条目 | 最近 20 条，16000 字符 |
| ~~旧格式检索~~ | 已移除 | 所有记忆通过 MEMORY.md 全文注入；手动 recall 为关键词搜索 | — |

**特点**：
- **全文注入**：SOUL.md, USER.md, MEMORY.md 始终全部注入（同 nanobot）
- **过渡层**：history.jsonl 未处理条目作为 "Recent History" 注入（同 nanobot）
- **无智能检索**：没有混合搜索、向量搜索、相关性选择——全部注入或简单关键词匹配
- **无 Chunk 级召回**：返回完整文件内容，不支持文本块级检索

### 召回到上下文维度小结

| 维度 | nanobot | Claude Code | OpenClaw | mybot |
|------|---------|-------------|----------|-------|
| **检索方式** | 全文注入 | Sonnet sideQuery 语义选择 | 向量 + BM25 混合 + MMR 重排 | 全文注入 |
| **检索粒度** | 文件级 | 文件级（≤5 个，4KB each） | Chunk 级（SQLite 块） | 文件级 |
| **时间衰减** | 无 | Staleness 排序（辅助） | 指数衰减（30d 半衰期） | 无 |
| **去重** | 无 | 已展示记忆去重 | MMR 多样性 | 无 |
| **过渡层** | history.jsonl 未处理条目 | 无（Extraction 每轮直接写文件） | Memory Flush + Dream 各阶段 | history.jsonl 未处理条目 |
| **阻塞/异步** | 同步（提示词组装） | 异步 prefetch（非阻塞） | 同/异步混合（Active Memory 阻塞） | 同步（提示词组装） |
| **缓存** | memory_cache（session+query 分区） | System prompt 缓存 + SideQuery 独立 | 向量缓存（按 provider/model/hash） | memory_cache（同 nanobot） |
| **跨会话** | 是 | 是 | 是 | 是 |

---

## 四、核心设计理念对比

| 维度 | nanobot | Claude Code | OpenClaw | mybot |
|------|---------|-------------|----------|-------|
| **记忆写入模型** | 两级：Consolidator（实时 → JSONL）→ Dream（周期 → MEMORY.md, SOUL.md, USER.md, skills） | Prompt-driven：LLM 直接读写文件 + 后台 Extraction/Dream 辅助 | 多级：Agent 写入（手动）→ Memory Flush（自动）→ Dreaming（晋级） | 两级：Consolidator（实时 → JSONL）→ Dream（周期 → MEMORY.md, SOUL.md, USER.md） |
| **核心创新** | Dream 两阶段指令 + AgentRunner 文件编辑 | MEMORY.md 索引 + 个体文件 + 5 种记忆类型 | SQLite 混合搜索 + 三阶段 Dream + Chunk 级 | 参考 nanobot，简化 Phase 2 为程序化合并 |
| **搜索能力** | 无（全文注入） | Sonnet sideQuery 语义选择（≤5 文件） | 向量 + BM25 混合 + MMR + 时间衰减 | 无（全文注入） |
| **存储引擎** | 文件系统（Markdown/JSONL） | 文件系统（Markdown + frontmatter） | 文件系统 + SQLite（FTS5 + sqlite-vec） | 文件系统（Markdown/JSONL） |
| **记忆类型数** | 4（USER/SOUL/MEMORY/SKILL） | 4（user/feedback/project/reference） | 多样化（MEMORY.md, daily notes, DREAMS.md, DREAMS, session transcripts） | 统一格式（MEMORY.md，旧 format 4 类型已移除） |
| **可扩展性** | 中（skill 系统是独特创新） | 高（SideQuery 语义选择很实用） | 最高（SQLite 支持大规模，MMR 保证多样性） | 低（全文注入，无检索） |
| **复杂度** | 中 | 高（5 层 CLAUDE.md + Extraction + Dream + Session Memory） | 最高（SQLite 后端 + 嵌入 + FTS + cron 三阶段 Dream） | 低（简洁，易于理解） |

---

## 五、mybot 的改进方向

基于以上对比，mybot 记忆系统可以按优先级改进：

### P1 — 短期可实施

1. ~~**Dream 添加去重逻辑**~~：已完成 — `_apply_adds()` 在写入前做不区分大小写的子串去重
2. ~~**Dream 添加行龄注释**~~：已完成 — `_update_age_annotations()` 为 MEMORY.md 中的行添加 `<- Nd` 标记
3. ~~**历史条目标记来源 session**~~：已完成 — `append_history()` 写入 session_key，Dream `_format_entries()` 展示给 LLM

### P2 — 中期计划

4. **混合搜索**：参考 OpenClaw 的 SQLite + sqlite-vec + FTS5 方案，为 MEMORY.md 和 history.jsonl 建立可搜索索引
5. **相关性筛选**：参考 Claude Code 的 Sonnet sideQuery 方案，用便宜模型在注入前筛选相关记忆
6. **时间衰减**：参考 OpenClaw 的指数衰减，让旧记忆在搜索中的权重自然下降

### P3 — 长期愿景

7. **Heartbeat 服务**：参考 nanobot 的 HeartbeatService + OpenClaw 的 HEARTBEAT.md，实现周期任务检查
8. **Skill 系统**：参考 nanobot 的 Dream Phase 1 `[SKILL]` 指令 + SKILL.md 文件，将重复工作流自动提取为可复用指令
9. **Chunk 级粒度**：大文件分块存储和检索，减少上下文浪费
