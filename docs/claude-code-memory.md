# Claude Code 记忆系统分析

## 概述

Claude Code 的记忆系统是一个**基于文件 + System Prompt 驱动的长期记忆模块**。与 mybot 的 store–manager–service 三层架构不同，Claude Code 将记忆的读写逻辑直接编码在 System Prompt 中——模型通过 Prompt 中的类型定义、保存规范、检索指引等指令，自行读写 `MEMORY.md` 索引和独立 `.md` 记忆文件。

核心设计理念：
- **Prompt 即 API**：记忆的 CRUD 规范全部在 System Prompt 中以自然语言描述，模型自己用 Write/Read 工具操作文件
- **MEMORY.md 是索引，不是内容**：每行一个 `<150` 字符的指针 `- [Title](file.md) — one-line hook`
- **LLM 做相关性过滤**：用 Sonnet sideQuery 从最多 200 个记忆中选择最相关的 5 个

## 与 mybot 的对比

| 维度 | Claude Code | mybot |
|------|-------------|-------|
| 架构 | Prompt 驱动 — 模型自行读写文件 | MemoryStore + Consolidator + Dream 管道 |
| 索引 | MEMORY.md 一行一个指针（~150 字符） | MEMORY.md 存储 Markdown 事实，非指针索引 |
| 检索 | LLM (Sonnet) 从 manifest 中选 top 5 | 全文注入 + 关键词匹配（recall） |
| 记忆种类 | user / feedback / project / reference | 统一格式（MEMORY.md，旧 4 类型已移除） |
| 团队记忆 | 支持 private + team 双目录 | 不支持 |
| 保存步骤 | 两步：写 .md 文件 + 追加 MEMORY.md 行 | ContextManager.remember() 直接追加到 MEMORY.md |
| 新鲜度感知 | memoryAge + staleness caveat + TRUSTING_RECALL | Dream._update_age_annotations（行龄注释 `<- Nd`） |
| 安全 | 路径遍历防护（null byte/symlink/Unicode） | 无特殊防护 |
| 会话模式 | 有 KAIROS assistant 长期会话 + 日志追加模式 | 无 |
| 提示词注入 | TYPES_SECTION 等结构化 XML 块注入 system prompt | _build_memory_context() 组装 Markdown |

## 目录布局

### Individual 模式（默认）

```
~/.claude/projects/<sanitized-git-root>/memory/
├── MEMORY.md              # 索引文件：每行一个指针
├── user/                  # 用户画像
│   └── <name>.md
├── feedback/              # 用户反馈
│   └── <name>.md
├── project/               # 项目上下文
│   └── <name>.md
└── reference/             # 外部系统引用
    └── <name>.md
```

### Team 模式（feature gate `TEAMMEM`）

```
~/.claude/projects/<sanitized-git-root>/memory/
├── MEMORY.md              # 私人索引
├── ...
└── team/                  # 团队共享目录
    ├── MEMORY.md          # 团队索引
    ├── user/
    ├── feedback/
    ├── project/
    └── reference/
```

### KAIROS Assistant 模式（feature gate `KAIROS`）

```
~/.claude/projects/<sanitized-git-root>/memory/
├── logs/
│   ├── 2026/
│   │   ├── 01/
│   │   │   ├── 2026-01-15.md    # 每日追加日志
│   │   │   └── 2026-01-16.md
│   ...
└── MEMORY.md                      # 由 nightly /dream skill 蒸馏生成
```

## 路径解析

`memdir/paths.ts`

### 解析顺序

```
getAutoMemPath():
  1. CLAUDE_COWORK_MEMORY_PATH_OVERRIDE 环境变量（Cowork/SDK 使用）
  2. autoMemoryDirectory in settings.json（policy > flag > local > user）
  3. <memoryBase>/projects/<sanitized-git-root>/memory/

getMemoryBaseDir():
  1. CLAUDE_CODE_REMOTE_MEMORY_DIR 环境变量（CCR 使用）
  2. ~/.claude（默认配置目录）
```

### 启用控制

```
isAutoMemoryEnabled():
  1. CLAUDE_CODE_DISABLE_AUTO_MEMORY=1/true → 关闭
  2. CLAUDE_CODE_DISABLE_AUTO_MEMORY=0/false → 开启
  3. CLAUDE_CODE_SIMPLE (--bare) → 关闭
  4. CCR 远程模式无 REMOTE_MEMORY_DIR → 关闭
  5. settings.json autoMemoryEnabled → 使用设置值
  6. 默认 → 开启
```

### 路径安全验证

`validateMemoryPath()` 多层防御：

```typescript
// memdir/paths.ts:109-150
function validateMemoryPath(raw: string | undefined, expandTilde: boolean): string | undefined {
  // 拒绝:
  // - 相对路径 (isAbsolute 检查)
  // - 根/近根路径 (length < 3) — "/" → "" 后太短
  // - Windows 盘根 (C: regex) — "C:\" → "C:" 
  // - UNC 路径 (\\server\share) — 不透明信任边界
  // - null byte — 在 C syscall 中会被截断
  // - ~/ 展开后仅 "." 或 ".." — 指向 $HOME 或上级目录
}
```

项目路径本身也经过清洗：使用 `findCanonicalGitRoot()` 确保同一仓库的所有 worktree 共享一份记忆目录。

## 四种记忆类型

`memdir/memoryTypes.ts`

```typescript
export const MEMORY_TYPES = ['user', 'feedback', 'project', 'reference'] as const
```

### 类型定义（以 individual 模式为例）

四种类型在 System Prompt 中以结构化 XML 块注入：

| 类型 | 用途 | when_to_save | body_structure |
|------|------|-------------|----------------|
| `user` | 用户角色/偏好/知识 | 了解用户任何细节时 | 无特殊要求 |
| `feedback` | 用户给的行为指导 | 用户纠正 OR 确认非显然做法 | `Why:` + `How to apply:` |
| `project` | 项目目标/决策/事故 | 了解谁在做什么、为什么、何时 | `Why:` + `How to apply:` |
| `reference` | 外部系统指针 | 了解外部资源位置时 | 无特殊要求 |

### What NOT to save（排除清单）

```typescript
// memoryTypes.ts:183-195
export const WHAT_NOT_TO_SAVE_SECTION = [
  '- Code patterns, conventions, architecture, file paths, or project structure',
  '- Git history, recent changes, or who-changed-what',
  '- Debugging solutions or fix recipes',
  '- Anything already documented in CLAUDE.md files.',
  '- Ephemeral task details: in-progress work, temporary state',
  // 关键规则：即使用户明确要求保存，这些排除也适用
  'These exclusions apply even when the user explicitly asks you to save.',
]
```

### MEMORY.md 索引格式

每行一个约 150 字符的 Markdown 链接：

```
- [language-preference](user/language-preference.md) — user prefers Chinese responses
- [integration-tests-real-db](feedback/integration-tests-real-db.md) — integration tests must hit real DB
```

### Before recommending from memory（记忆漂移防护）

```typescript
// memoryTypes.ts:240-256
export const TRUSTING_RECALL_SECTION = [
  '## Before recommending from memory',
  'A memory that names a specific function, file, or flag is a claim that it existed *when the memory was written*.',
  '- If the memory names a file path: check the file exists.',
  '- If the memory names a function or flag: grep for it.',
  '"The memory says X exists" is not the same as "X exists now."',
]
```

这是经过 eval 验证的设计——模型必须验证记忆中的代码级声明（文件路径、函数名）在当前代码库中是否仍然有效。

## Prompt 构建流程

`memdir/memdir.ts`

### loadMemoryPrompt() — 入口

```typescript
// memdir/memdir.ts:419-507
export async function loadMemoryPrompt(): Promise<string | null> {
  // 分发逻辑:
  // 1. KAIROS + autoEnabled → buildAssistantDailyLogPrompt()
  // 2. TEAMMEM + teamEnabled → buildCombinedMemoryPrompt()
  // 3. autoEnabled → buildMemoryLines() (individual 模式)
  // 4. 全部禁用 → 返回 null，记录 telemetry
}
```

### buildMemoryLines() — Individual 模式 Prompt

```typescript
// memdir/memdir.ts:199-266
export function buildMemoryLines(displayName, memoryDir, extraGuidelines?, skipIndex?): string[] {
  return [
    `# ${displayName}`,
    `You have a persistent, file-based memory system at \`${memoryDir}\`. ${DIR_EXISTS_GUIDANCE}`,
    ...TYPES_SECTION_INDIVIDUAL,     // 四种类型定义
    ...WHAT_NOT_TO_SAVE_SECTION,      // 不应保存的内容
    ...howToSave,                     // 保存步骤（两步 or 一步）
    ...WHEN_TO_ACCESS_SECTION,        // 何时访问记忆
    ...TRUSTING_RECALL_SECTION,       // 如何信任回忆的内容
    '## Memory and other forms of persistence',  // 与其他持久化机制的区分
    ...buildSearchingPastContextSection(memoryDir),  // 搜索过去上下文
  ]
}
```

### 保存步骤的两种模式

**标准模式**（skipIndex=false）：两步保存
1. 写 `.md` 文件到对应类型子目录
2. 在 `MEMORY.md` 中追加一行指针

**skipIndex 模式**（feature gate `tengu_moth_copse`）：一步保存
- 只写 `.md` 文件，不维护 MEMORY.md 索引

### buildMemoryPrompt() — 带 MEMORY.md 内容

```typescript
// memdir/memdir.ts:272-316
export function buildMemoryPrompt(params): string {
  // 1. 同步读取 MEMORY.md
  // 2. 调用 buildMemoryLines() 生成指令
  // 3. 如果 MEMORY.md 非空 → 追加内容（经过 truncation）
  // 4. 如果 MEMORY.md 为空 → 追加空状态提示
}
```

### Entrypoint 截断

```typescript
// memdir/memdir.ts:34-38
export const MAX_ENTRYPOINT_LINES = 200
export const MAX_ENTRYPOINT_BYTES = 25_000

// memdir/memdir.ts:57-103
export function truncateEntrypointContent(raw: string): EntrypointTruncation {
  // 1. 先按行截断（200 行）
  // 2. 再按字节截断（25KB，在最后一个换行符处切断）
  // 3. 附加警告信息，告知哪个限制被触发
}
```

双重截断的原因是：200 行上限覆盖常见情况，25KB 字节上限防止单行异常长的索引条目（实测 p100: 197KB 仅 200 行）。

### DIR_EXISTS_GUIDANCE

```typescript
// memdir/memdir.ts:116-119
export const DIR_EXISTS_GUIDANCE =
  'This directory already exists — write to it directly with the Write tool (do not run mkdir or check for its existence).'
```

这个设计是因为模型倾向于在执行写操作前先 `ls`/`mkdir -p` 确认目录存在，浪费一轮调用。在 `loadMemoryPrompt()` 中通过 `ensureMemoryDirExists()` 预先创建目录，并告知模型无需检查。

## LLM 相关性过滤

`memdir/findRelevantMemories.ts`

Claude Code 不使用关键词检索或 embedding 检索，而是用 **Sonnet sideQuery** 做记忆选择：

```typescript
// findRelevantMemories.ts:39-75
export async function findRelevantMemories(
  query: string,
  memoryDir: string,
  signal: AbortSignal,
  recentTools: readonly string[] = [],
  alreadySurfaced: ReadonlySet<string> = new Set(),
): Promise<RelevantMemory[]> {
```

### 流程

1. **scanMemoryFiles()** — 扫描目录，读取所有 `.md` 文件的 frontmatter（最多 30 行），得到 name/description/type/mtime 清单
2. **过滤** — 排除 `MEMORY.md` 和已在前几轮展示过的记忆
3. **formatMemoryManifest()** — 格式化为一行一个记忆的文本清单
4. **selectRelevantMemories()** — 调用 Sonnet sideQuery，传入 query + manifest，要求 JSON schema 输出最多 5 个文件名

### Selector Prompt

```
You are selecting memories that will be useful to Claude Code as it processes 
a user's query. You will be given the user's query and a list of available 
memory files with their filenames and descriptions.

Return a list of filenames for the memories that will clearly be useful 
(up to 5). Only include memories that you are certain will be helpful.
- If you are unsure, do not include it.
- If there are no clearly useful memories, return an empty list.
- If recently-used tools are provided, DO NOT select usage reference or API 
  docs for those tools. DO still select memories with warnings/gotchas 
  about those tools.
```

关键设计细节：
- **最近使用工具过滤**：如果某些工具正在活跃使用中，不选择它们的使用文档（因为对话中已有），但仍选择关于它们的警告/陷阱记忆
- **已展示去重**：`alreadySurfaced` 参数过滤前几轮中已经被展示过的记忆，让 5 个名额全部分配给新候选者

## 记忆新鲜度

`memdir/memoryAge.ts`

```typescript
export function memoryAgeDays(mtimeMs: number): number {
  return Math.max(0, Math.floor((Date.now() - mtimeMs) / 86_400_000))
}

export function memoryAge(mtimeMs: number): string {
  const d = memoryAgeDays(mtimeMs)
  if (d === 0) return 'today'
  if (d === 1) return 'yesterday'  
  return `${d} days ago`
}

export function memoryFreshnessText(mtimeMs: number): string {
  const d = memoryAgeDays(mtimeMs)
  if (d <= 1) return ''
  return `This memory is ${d} days old. Memories are point-in-time observations, 
           not live state — claims about code behavior or file:line citations 
           may be outdated. Verify against current code before asserting as fact.`
}
```

设计理由（代码注释）：模型不擅长日期算术——原始 ISO 时间戳不会像 "47 days ago" 那样触发过时推理。

## 团队记忆

`memdir/teamMemPaths.ts` + `memdir/teamMemPrompts.ts`

### 目录结构

团队记忆是私人记忆目录下的 `team/` 子目录，通过 feature gate `TEAMMEM` 启用。

### 双重模式 Prompt

`buildCombinedMemoryPrompt()` 同时描述私人目录和团队目录，每个类型带有 `<scope>` 指导：

```xml
<type>
    <name>user</name>
    <scope>always private</scope>
    ...
</type>
<type>
    <name>feedback</name>
    <scope>default to private. Save as team only when ...</scope>
    ...
</type>
<type>
    <name>project</name>
    <scope>private or team, but strongly bias toward team</scope>
    ...
</type>
<type>
    <name>reference</name>
    <scope>usually team</scope>
    ...
</type>
```

### 路径遍历防护

`validateTeamMemWritePath()` 和 `validateTeamMemKey()` 实现双重验证：

1. **第一遍（字符串）**：`path.resolve()` 消除 `..` 段，做字符串级 containment 检查
2. **第二遍（文件系统）**：`realpathDeepestExisting()` 解析最深存在祖先的符号链接，比较真实路径和真实团队目录

`sanitizePathKey()` 额外检测：
- null byte（C syscall 截断攻击）
- URL 编码的路径穿越（`%2e%2e%2f`）
- Unicode 规范化攻击（全角 `．．／` 经 NFKC 变为 `../`）
- 反斜杠（Windows 分隔符）
- 绝对路径

`realpathDeepestExisting()` 处理复杂情况：目标文件可能尚不存在，所以从文件路径向上遍历，找到最深存在祖先，解析其符号链接，再拼接不存在的尾部。

## 会话模式差异

### 标准模式

- 每次保存两步：写 `.md` → 更新 `MEMORY.md`
- MEMORY.md 作为"实时索引"
- 模型可以随时编辑和重组记忆

### KAIROS Assistant 模式

`buildAssistantDailyLogPrompt()`:

- 长期运行会话，通过日期变更附件感知日期变化
- 新的记忆**追加**到 `logs/YYYY/MM/YYYY-MM-DD.md`（每日日志文件）
- 不直接编辑 MEMORY.md
- 单独的 nightly `/dream` skill 将日志蒸馏为主题文件 + 更新 MEMORY.md
- 索引仍在上下文加载（由 claudemd.ts 处理），但模型被指示不要直接编辑它

## 搜索过去上下文

`buildSearchingPastContextSection()`:

```typescript
// memdir/memdir.ts:375-407
export function buildSearchingPastContextSection(autoMemDir: string): string[] {
  // 提供两个 grep 命令模板：
  // 1. 搜索记忆文件：grep -rn "<term>" <memDir> --include="*.md"
  // 2. 搜索会话转录：grep -rn "<term>" <projectDir>/ --include="*.jsonl"
  // 使用窄搜索词（错误消息/文件路径/函数名）而非宽泛关键词
}
```

## eval 验证的设计

代码中有多处 eval-validated 注释，说明设计选择经过定量验证：

- **TRUSTING_RECALL section 位置**：作为独立 section（标题 "Before recommending from memory"）测试得分 3/3；作为 "When to access" 的子项目标得分 0/3——同一正文，位置决定了模型行为
- **WHAT_NOT_TO_SAVE explicit-save gate**："即使用户明确要求保存，排除也适用"这行让 `memory-prompt-iteration case 3` 从 0/2 提升到 3/3
- **"ignore memory" bullet**："如果用户说要 ignore，就当 MEMORY.md 为空来处理"防止一种特定失败模式：模型读取正确的代码后却附加 "not Y as noted in memory"——命名了反模式

## 设计要点

- **Prompt 即接口**：整个记忆系统的 API 是 System Prompt 中的自然语言指令，无需额外代码层
- **MEMORY.md 是指针索引**：每行 150 字符，最多 200 行 / 25KB，保持上下文负担可控
- **LLM-powered 检索**：用侧路 Sonnet 调用做记忆选择，天然语义理解，无需维护 embedding 基础设施
- **记忆漂移防御**：多层验证——新鲜度警告 + TRUSTING_RECALL 验证指引 + 过时记忆更新
- **团队记忆有安全墙**：双重路径验证（resolve + realpath）、符号链接检测、Unicode 规范化攻击防御、dangling symlink 检测
- **Eval 驱动迭代**：提示词中的措辞、位置、结构都经过定量 eval 验证，而非凭直觉编写
- **目录即保证**：`ensureMemoryDirExists()` 预先创建目录，配合 DIR_EXISTS_GUIDANCE 告知模型，节省一轮 `ls`/`mkdir` 调用
- **跨 worktree 共享**：`findCanonicalGitRoot()` 确保同一 git 仓库的所有 worktree 共享一份记忆
