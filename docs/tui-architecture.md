# TUI 架构 (Textual Chat UI)

## 概述

mybot 的 TUI 基于 [Textual](https://textual.textualize.io/) 8.2.7 构建，是一个全屏终端聊天界面。它替代了旧的 `prompt_toolkit` 交互循环 + `StreamRenderer`，实现了非阻塞流式渲染、鼠标滚轮支持、输入历史持久化和模态弹窗确认。

核心设计原则：**`Orchestrator.process_message()` 不变，只替换渲染层**。Agent 执行、工具调用、LLM 通信全部复用现有逻辑。

## 文件结构

```
tui/
├── __init__.py    # 包入口，导出 ChatApp
├── app.py         # ChatApp 主应用（~458 行）
├── widgets.py     # 自定义 Widget 组件（~354 行）
├── screens.py     # 模态弹窗（~107 行）
└── theme.css      # 暗色主题（~72 行）
```

## 屏幕布局

整个应用使用垂直流式布局，从上到下依次为：

```
┌─────────────────────────────────────────────┐
│  Header (#header-bar)          dock: top    │  height: 1
├─────────────────────────────────────────────┤
│                                             │
│  VerticalScroll (#chat-area)                │  height: 1fr（占据剩余空间）
│    ├── <banner>                             │
│    ├── [Spacer | UserMessage]               │  用户消息右对齐
│    ├── [StreamingMessage | Spacer]          │  流式响应左对齐
│    ├── [ErrorMessage | Spacer]              │  错误消息左对齐
│    └── ...                                  │
│                                             │
├─────────────────────────────────────────────┤
│  SessionStatus (#session-status)            │  height: auto（空闲 0，活跃 1）
│    ● 准备中... / 生成回复中... / 工具执行中  │  闪烁白点 + 状态文本
├─────────────────────────────────────────────┤
│  Horizontal (#input-area)      dock: bottom │
│    Input (#user-input)                      │
├─────────────────────────────────────────────┤
│  StatusFooter (#status-bar)                 │  height: 1
├─────────────────────────────────────────────┤
│  Footer                        自动 dock    │  height: 1（快捷键提示）
└─────────────────────────────────────────────┘
```

**关键 CSS**（`theme.css`）：

```css
#chat-area {
    height: 1fr;           /* 占据所有剩余空间 */
    overflow-y: auto;      /* 内容超出时显示垂直滚动条 */
    scrollbar-size-vertical: 2;
}

#input-area {
    dock: bottom;          /* 固定在底部 */
}

.chat-row {
    width: 100%;            /* 填充全宽 */
    height: auto;           /* 根据内容自适应高度 */
}
```

## 消息行模型

左右对齐的核心机制：`Horizontal` 容器 + `ChatSpacer`（`width: 1fr` 弹性填充）。

```
用户消息（右对齐）：
  Horizontal(.chat-row)
    ├── ChatSpacer(1fr)  ← 弹性填充，将 UserMessage 推到右侧
    └── UserMessage(max-width: 72%)

助手消息（左对齐）：
  Horizontal(.chat-row)
    ├── StreamingMessage(max-width: 88%)  ← 自然靠左
    └── ChatSpacer(1fr)  ← 弹性填充剩余空间
```

`ChatSpacer` 是一个 `Static` 空组件，CSS 为 `width: 1fr; height: auto;`，负责吸收行内多余空间，实现气泡靠边效果。

## Widget 组件详解

### 继承层级

```
Static (Textual 内置)
  ├── _Bubble          ← 基类：width: auto; max-width: 88%; height: auto
  │     ├── UserMessage       ← 蓝色 Panel 气泡
  │     ├── AssistantMessage  ← Markdown 渲染
  │     ├── StreamingMessage  ← reactive 流式渲染
  │     ├── ToolStatus        ← 工具执行闪烁指示器（预留，未参与当前 compose）
  │     └── ErrorMessage      ← 红色错误信息
  ├── SessionStatus    ← 会话状态栏（闪烁白点 + 阶段文本）
  ├── ChatSpacer        ← 弹性填充 spacer
  └── StatusFooter      ← 单行状态栏
```

### _Bubble — 气泡基类

```python
class _Bubble(Static):
    DEFAULT_CSS = """
    _Bubble {
        width: auto;        # 宽度由内容决定
        max-width: 88%;     # 不超过屏幕的 88%
        height: auto;       # 高度由内容决定
    }
    """
```

所有聊天消息组件都继承 `_Bubble`，确保统一的气泡约束。`DEFAULT_CSS` 是 Textual 的组件级样式机制，样式随组件定义，不需要在全局 CSS 中重复声明。

### UserMessage — 用户消息气泡

```python
class UserMessage(_Bubble):
    DEFAULT_CSS = """UserMessage { max-width: 72%; }"""

    def __init__(self, content: str, **kwargs) -> None:
        text = Text(content)
        text.stylize("bold")                                      # 粗体文本
        panel = Panel(text, border_style="bright_blue", padding=(0, 1))
        super().__init__(panel, **kwargs)
```

使用 Rich `Panel` 包裹，亮蓝色边框模拟聊天气泡。`max-width: 72%` 覆盖基类的 88%，让用户消息比助手消息更窄。

### AssistantMessage — 助手消息

```python
class AssistantMessage(_Bubble):
    def __init__(self, content: str, **kwargs) -> None:
        super().__init__(Markdown(content), **kwargs)
```

直接使用 Rich `Markdown` 渲染，支持代码块、列表、表格等完整 Markdown 语法。无边框，与蓝色用户气泡形成视觉对比。

### StreamingMessage — 流式渲染

这是 TUI 中**最复杂的组件**，负责在 LLM token 逐字到达时实时渲染。

**核心机制**：`reactive` + 节流。

```python
class StreamingMessage(_Bubble):
    _THROTTLE = 0.08  # ~12 FPS — 节流间隔

    content = reactive("")  # Textual reactive 属性

    def __init__(self, **kwargs) -> None:
        super().__init__("", **kwargs)
        self._pending = ""         # 累积缓冲区
        self._last_update = 0.0    # 上次渲染时间戳

    def add_token(self, token: str) -> None:
        """累积 token 并节流渲染。"""
        self._pending += token
        now = time.monotonic()
        # 条件：距上次渲染 >= 80ms，或缓冲区 < 200 字符（初始快速显示）
        if now - self._last_update >= self._THROTTLE or len(self._pending) < 200:
            self.content = self._pending    # 赋值触发 reactive → watch_content
            self._last_update = now

    def watch_content(self, content: str) -> None:
        """reactive 自动调用 — 渲染 Markdown。"""
        if content:
            self.update(Markdown(content))

    def _refresh(self) -> None:
        """强制立即渲染 — 用于工具调用开始/失败等离散事件。"""
        self.content = self._pending
        self._last_update = time.monotonic()

    def finish(self) -> None:
        """最终提交 — 确保所有 pending token 都被渲染。"""
        self.content = self._pending
```

**设计决策**：

| 决策 | 原因 |
|------|------|
| `reactive("")` 而非手动 `update()` | 声明式更新，Textual 自动管理刷新时机 |
| 0.08s 节流 | 防止每个 token（可能每秒 100+）触发一次布局重算 |
| `< 200` 字符不节流 | 开头几个 token 立即显示，避免用户看到空白 |
| `_refresh()` 独立于 `add_token()` | 工具调用事件不与 token 流节奏同步，需要强制立即刷新 |

### ToolStatus — 工具执行闪烁指示器

预挂载的独立工具状态 widget，通过 `height: auto` + 空内容实现零高度空闲态，无需 CSS `display` 切换即可在 `@work` 上下文中立即渲染。

**状态机**：

```
activate(name, args_brief)  →  闪烁中（● / ○ 交替）
    ├── mark_done()          →  静态白点 ● + 工具名
    ├── mark_error(detail)   →  红色 ● + 工具名 + 错误详情（最多 100 字符）
    └── deactivate()         →  空内容（height: auto → 高度归零）
```

```python
class ToolStatus(_Bubble):
    DEFAULT_CSS = """
    ToolStatus {
        max-width: 88%;
        height: auto;        # 空内容时高度归零，无需 display 切换
    }
    """
    _BLINK_INTERVAL = 0.4

    def on_mount(self) -> None:
        self._timer = self.set_interval(self._BLINK_INTERVAL, self._blink)

    def activate(self, name, args_brief="") -> None:
        """绑定工具并开始闪烁。"""
        self._tool_name = name
        self._done = False
        self.update(self._build())

    def deactivate(self) -> None:
        """重置为空内容 → height:auto → 高度归零。"""
        self._done = True
        self.update(Text(""))

    async def mark_done(self) -> None:
        """停止闪烁，保留白点。"""
        self._done = True
        self.update(self._build())

    async def mark_error(self, detail="") -> None:
        """白点变红，附加错误详情。"""
        ...

    def _blink(self) -> None:
        if self._done: return
        self._dot_on = not self._dot_on
        self.update(self._build())
```

**当前使用情况**：`ToolStatus` 和配套的 `ToolDock`（`Vertical` 容器，`max-height: 14`）已完整实现但未参与当前 `compose()` 布局。当前工具执行指示由 `SessionStatus` 状态栏（显示"工具执行中 (N)"）和 `StreamingMessage` 中的内联 `⚙ **name**(args)` 标记联合提供。`ToolStatus` 保留用于未来需要独立逐工具闪烁指示的场景。

### SessionStatus — 会话状态栏

位于聊天区和输入栏之间的动态状态指示器。通过 `set_interval` 定时器驱动 `●` / `○` 交替闪烁，向用户传达"会话正在执行中"的信号，避免消息处理期间界面无反馈导致的卡顿错觉。

```python
class SessionStatus(Static):
    DEFAULT_CSS = """
    SessionStatus {
        height: auto;        # 空闲时内容为空 → 高度归零，不占空间
        padding: 0 1;
    }
    """
    _BLINK_INTERVAL = 0.5  # 500ms 闪烁周期

    def show(self, status="准备中...") -> None:
        """激活状态栏并设置初始文本。"""
        self._active = True
        self._status = status
        self._dot_on = True
        self._redraw()

    def set_status(self, status: str) -> None:
        """更新状态文本（如 "工具执行中 (3)"）。"""
        self._status = status
        self._redraw()

    def hide(self) -> None:
        """折叠状态栏 — 清空内容使 height:auto 归零。"""
        self._active = False
        self._status = ""
        self.update(Text(""))

    def _redraw(self) -> None:
        if not self._active or not self._status:
            self.update(Text("")); return
        dot = "●" if self._dot_on else "○"
        text = Text(f"  {dot} ", style="bold white")
        text.append(self._status, style="dim")
        self.update(text)
```

**状态枚举**：

| 状态文本 | 触发时机 |
|---------|---------|
| `准备中...` | 用户提交消息后立即显示 |
| `思考中...` | LLM 进入 extended-thinking 阶段 |
| `生成回复中...` | LLM 流式输出文本 token |
| `工具调用中...` | SSE 报告 tool_call delta（工具即将执行） |
| `工具执行中 (N)` | N 个工具正在并行执行 |

**设计要点**：
- `height: auto` + 空内容 = 零高度，空闲时完全不可见，无布局抖动
- 闪烁由 `set_interval` 驱动，在 Textual 主事件循环上运行，与 `@work` 并行
- `set_status()` 随时可调用（包括 `@work` 上下文），因为 `Static.update()` 不触发布局重算
- 工具执行计数 `_tool_count` 在 `_run_chat` 闭包中维护，`_on_tool_exec_start` 递增、`_on_tool_exec_end` 递减

### StatusFooter — 状态栏

```python
class StatusFooter(Static):
    DEFAULT_CSS = """
    StatusFooter {
        height: 1; padding: 0 1;
        background: $surface;       # Textual 内置 token
    }
    """

    def set_usage(self, session_key="", prompt_tokens=0,
                  completion_tokens=0, elapsed_ms=0, paradigm="") -> None:
        parts = []
        if session_key:
            parts.append(f"Session: {session_key}")
        if prompt_tokens or completion_tokens:
            parts.append(f"Tokens: {prompt_tokens:,} in / {completion_tokens:,} out")
        if elapsed_ms:
            parts.append(f"{elapsed_ms / 1000:.1f}s")
        if paradigm:
            parts.append(f"[{paradigm}]")
        self._text = Text("  ".join(parts), style="dim italic")
        self.update(self._text)
```

运行时显示：`Session: 20260628-143000  Tokens: 1,234 in / 567 out  3.2s  [react]`

### ErrorMessage — 错误提示

```python
class ErrorMessage(_Bubble):
    def __init__(self, error_text: str, **kwargs) -> None:
        text = Text(f"✗ {error_text}", style="bold red")
        super().__init__(text, **kwargs)
```

红色粗体，前缀 `✗`。当 `_run_chat` 捕获 `Exception` 或结果包含 error 时挂载到聊天区。

## 主应用 ChatApp

### 初始化与启动

```python
class ChatApp(App):
    CSS_PATH = "theme.css"   # 全局样式文件
    BINDINGS = [
        ("ctrl+c", "quit_or_copy", "Quit / Copy"),
        Binding("escape", "cancel_message", "Cancel", show=False, priority=True),
        Binding("up", "history_prev", "", show=False, priority=True),
        Binding("down", "history_next", "", show=False, priority=True),
    ]
```

`Orchestrator.main()` 中启动：

```python
async def _run() -> None:
    await orche.start_services()     # 启动 cron、事件总线等后台服务
    app = ChatApp(orchestrator=orche, session_key=session_key,
                  model=model, is_resumed=is_resumed)
    await app.run_async()           # 阻塞直到用户退出
    await orche.stop_services()

asyncio.run(_run())
```

### 消息处理流程

```
用户输入 (Enter)
    ↓
on_input_submitted()
    ├── /exit, /quit  →  _confirm_exit()  →  ConfirmScreen  →  exit()
    ├── /xxx          →  _handle_slash_command()
    └── 普通消息
          ├── 保存输入历史（去重）
          ├── input.disabled = True（防重复提交）
          ├── 挂载 UserMessage bubble 到聊天区
          ├── 挂载 StreamingMessage 到聊天区
          ├── session_status.show("准备中...")  ← 激活闪烁状态栏
          └── self._run_chat(text, stream, chat)  ← 非阻塞 Worker
```

### Worker 流式执行

```python
@work(exclusive=True, group="chat")
async def _run_chat(self, text, stream, chat):
```

`@work` 装饰器将方法转为后台协程，关键参数：

- **`exclusive=True`**：同一 group 中最多一个 worker，新消息自动取消旧 worker（替代手动 CancelToken）
- **`group="chat"`**：分组标识，`action_cancel_message()` 通过 `w.group == "chat"` 查找并取消

**7 个流式回调**（均通过闭包更新 SessionStatus 状态栏）：

| 回调 | 触发时机 | 行为 |
|------|---------|------|
| `_on_delta(token)` | 每个 content token | 设置阶段为"生成回复中..."，更新状态栏；累积到 stream，0.15s 节流滚动到底部 |
| `_on_thinking(token)` | 每个 reasoning token | 设置阶段为"思考中..."，更新状态栏 |
| `_on_thinking_done()` | 推理完成 | 设置阶段为"生成回复中..."，更新状态栏 |
| `_on_tool_start(name, brief)` | SSE tool_call delta | 设置阶段为"工具调用中..."，更新状态栏 |
| `_on_tool_exec_start(name, args, idx, total)` | 工具开始执行 | `_tool_count += 1`，更新状态栏为"工具执行中 (N)"；`stream.add_token("⚙ **name**(args)")` + 强制刷新 |
| `_on_tool_exec_end(ev)` | 工具执行完成 | `_tool_count -= 1`，更新状态栏；失败时 `stream.add_token("❌ detail")` + 强制刷新 |
| `_on_new_turn()` | Agent 内部新轮次 | 设置阶段为"生成回复中..."；`stream.add_token("\n\n")` 分隔轮次 |

**SessionStatus 渲染逻辑**（闭包内 `_render_bar()`）：

```python
_bar = self.query_one("#session-status", SessionStatus)
_tool_count = 0
_phase = "生成回复中..."

def _render_bar() -> None:
    if _tool_count > 0:
        _bar.set_status(f"工具执行中 ({_tool_count})")
    else:
        _bar.set_status(_phase)
```

工具计数优先：有工具在执行时显示计数，否则显示当前阶段文本。

**错误处理**：

```python
try:
    result = await self._orche.process_message(...)
except asyncio.CancelledError:
    stream.add_token("\n\n*[cancelled]*\n")       # 用户按 Escape
    return
except Exception as exc:
    await chat.mount(self._error_row(str(exc)))    # 显示错误 bubble
    return
finally:
    _bar.hide()                                      # 折叠 SessionStatus 状态栏
    input_w.disabled = False                         # 恢复输入
    input_w.focus()
```

**完成处理**：

```python
stream.finish()                                    # 提交所有 pending token
if not result.content and result.error:
    await chat.mount(self._error_row(result.error))
# 更新状态栏
self.query_one("#status-bar", StatusFooter).set_usage(
    session_key=self._session_key,
    prompt_tokens=usage.get("prompt_tokens", 0),
    completion_tokens=usage.get("completion_tokens", 0),
    elapsed_ms=elapsed,
    paradigm=result.paradigm or "",
)
```

### 快捷键系统

| 按键 | 方法 | 行为 |
|------|------|------|
| `Ctrl+C` | `action_quit_or_copy()` | 有选中文本 → 复制到剪贴板；无 → 退出应用 |
| `Escape` | `action_cancel_message()` | 输入框有焦点 → 清空输入；否则 → 取消 worker |
| `Up` | `action_history_prev()` | 向前遍历输入历史（仅输入框有焦点时） |
| `Down` | `action_history_next()` | 向后遍历输入历史 |
| 鼠标滚轮 | — | 滚动 #chat-area（VerticalScroll 原生处理） |
| `Shift+拖动` | — | 原生终端文本选择（Shift 绕过应用鼠标捕获） |

### 输入历史

持久化到 `workspace/sessions/{session_key}_input_history.json`：

```python
def _load_history(self) -> None:
    data = json.loads(self._history_path.read_text(encoding="utf-8"))
    self._input_history = data[-1000:]  # 截断保留最新 1000 条

def _save_history(self) -> None:
    self._history_path.write_text(json.dumps(self._input_history))

# 使用：连续相同输入不重复记录
if not self._input_history or self._input_history[-1] != text:
    self._input_history.append(text)
    self._save_history()
```

导航逻辑：`Up` 从最新 → 最旧遍历；`Down` 反向遍历；到达边界后回到用户正在输入的内容（`_saved_input`）。

## 模态弹窗

### ConfirmScreen — 确认弹窗

```python
class ConfirmScreen(ModalScreen[bool]):
    """居中确认弹窗，返回 True/False。"""
    DEFAULT_CSS = """
    ConfirmScreen { align: center middle; }
    ConfirmScreen #dialog {
        width: 40; border: thick $accent;
        background: $surface; padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        with Container(id="dialog"):
            yield Label(self._message)
            with Horizontal():
                yield Button("Yes", variant="primary", id="yes")
                yield Button("No", variant="error", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")   # 返回 True/False
```

调用方式：

```python
def _confirm_exit(self) -> None:
    self.push_screen(
        ConfirmScreen("Are you sure you want to quit?"),
        lambda result: self.exit() if result else None,
    )
```

`push_screen(screen, callback)` 是非阻塞的——弹窗显示，用户选择后 callback 执行。`ModalScreen[bool]` 的泛型参数告诉类型检查器 callback 接收 `bool | None`。

### SessionListScreen — 会话列表弹窗

```python
class SessionListScreen(ModalScreen[None]):
    def compose(self) -> ComposeResult:
        with Container(id="dialog"):
            yield Label(f"Sessions ({len(self._sessions)})")
            with VerticalScroll(id="sessions-list"):
                yield ListView(id="session-list")
            yield Button("Close", variant="primary", id="close")

    def on_mount(self) -> None:
        lst = self.query_one("#session-list", ListView)
        for sess in self._sessions:
            key = sess.get("key", "?")
            updated = str(sess.get("updated_at", ""))[:16]
            lst.append(ListItem(Label(f"{key}  [{updated}]")))
```

## 斜杠命令

| 命令 | 行为 |
|------|------|
| `/exit`, `/quit` | 弹出 ConfirmScreen 确认后退出 |
| `/clear` | 弹出 ConfirmScreen 确认后移除聊天区所有子组件 |
| `/help` | 显示当前 session key 和 model |
| `/history` | 显示当前 session 的消息数和交换轮数 |
| `/sessions` | 弹出 SessionListScreen 显示所有 session |

## 主题设计

### 配色方案（GitHub 暗色主题风格）

| 颜色 | 用途 |
|------|------|
| `#0d1117` | 主背景 |
| `#161b22` | 表面/面板背景（Header, Input, Footer, 滚动条） |
| `#58a6ff` | 主色调（Input focus 边框, 用户气泡, 滚动条） |
| `#f0883e` | 强调色（Input hover 边框, 滚动条 hover） |
| `#c9d1d9` | 主文本颜色 |
| `#8b949e` | 次要文本（Footer, StatusFooter） |
| `#30363d` | 边框颜色 |
| `#f85149` | 错误颜色 |

### CSS 组织

- **`theme.css`**：布局级样式（Screen 背景、#chat-area 滚动条、#input-area dock、伪类等）
- **`DEFAULT_CSS`**：组件级样式（每个 Widget 类自带，如 `_Bubble { width: auto; max-width: 88% }`）

这种分离确保组件自包含——新增组件不需要修改全局 CSS。

**注意**：Textual 8.2.7 不支持 CSS 自定义属性（`--var: value` / `var(--var)`），只支持 `$` 前缀的内置 token（如 `$surface`、`$primary`、`$accent`）。因此 `theme.css` 中使用硬编码颜色值。

## 与 Orchestrator 集成

```
orchestrator.main()
  ├── 解析 CLI 参数（-c / --continue, --debug）
  ├── 创建 OpenAICompatibleProvider
  ├── 创建 Orchestrator(workspace, provider, compress_model, log_config)
  ├── 确定 session_key（-c 恢复 / 新建）
  ├── 写入 .last_session
  ├── 创建 ChatApp(orchestrator, session_key, model, is_resumed)
  └── asyncio.run(app.run_async())
        ├── orche.start_services()   # cron + 事件总线
        ├── app.run_async()          # Textual 事件循环
        └── orche.stop_services()
```

`ChatApp._run_chat()` 通过 `self._orche.process_message()` 调用 Orchestrator，传入 8 个回调函数。Orchestrator 内部的路由（Dispatcher → Agent.run → AgentCore.run → LLM chat_stream）完全透明，TUI 只关心回调接口。

## 代码调用链

### 应用启动

```
orchestrator.main()                                      # orchestrator.py:725
  │
  ├── 解析 CLI 参数 (-c / --continue, --debug, --model)
  ├── 创建 OpenAICompatibleProvider
  ├── 创建 Orchestrator(workspace, provider, compress_model, log_config)
  ├── 确定 session_key (-c 恢复 / 新建)
  ├── 写入 .last_session
  │
  └── async def _run():                                  # orchestrator.py:758
        ├── await orche.start_services()                  # cron + 事件总线
        ├── app = ChatApp(orchestrator=orche,             # tui/app.py:62
        │                 session_key=session_key,
        │                 model=model, is_resumed=is_resumed)
        ├── await app.run_async()                         # Textual App.run_async()
        └── await orche.stop_services()
```

### ChatApp 初始化与 compose

```
ChatApp.__init__()                                       # app.py:62
  │
  ├── 存储 orchestrator, session_key, model, is_resumed
  ├── 设置 CSS_PATH = "theme.css"
  ├── 定义 BINDINGS:
  │     ctrl+c → quit_or_copy, escape → cancel_message,
  │     up/down → history_prev/next
  │
  └── compose()                                          # app.py:101
        ├── Header (#header-bar)         dock: top
        ├── VerticalScroll (#chat-area)  height: 1fr
        │     └── banner (首次显示)
        ├── SessionStatus (#session-status)  height: auto
        ├── Horizontal (#input-area)     dock: bottom
        │     └── Input (#user-input)
        └── StatusFooter (#status-bar)   height: 1
```

### 用户输入处理 → Worker 启动

```
on_input_submitted(event: Input.Submitted)                # app.py:186
  │
  ├── text = event.value.strip()
  ├── if /exit or /quit → _confirm_exit()
  │     └── push_screen(ConfirmScreen(message), callback)
  │           └── confirm → app.exit()
  │
  ├── if /xxx (slash command) → _handle_slash_command()   # app.py:210
  │     ├── /clear  → push_screen(ConfirmScreen) → remove children
  │     ├── /help   → mount info message
  │     ├── /history → mount message count
  │     └── /sessions → push_screen(SessionListScreen)
  │
  └── else (普通消息):
        ├── _save_history(text)  # 持久化输入历史 (去重)
        ├── input.disabled = True  # 防重复提交
        ├── chat.mount(UserMessage(text))                # app.py:200
        │     └── _Bubble → Panel(text, border_style="bright_blue") # widgets.py:121
        ├── stream = StreamingMessage()                   # widgets.py:150
        ├── chat.mount(stream)
        ├── session_status.show("准备中...")              # widgets.py:261
        │     └── _active=True, _redraw() → "● 准备中..."
        └── self._run_chat(text, stream, chat)  ← @work  # app.py:228
```

### Worker 流式执行（@work exclusive）

```
@work(exclusive=True, group="chat")
async def _run_chat(text, stream, chat)                   # app.py:228
  │
  │  # exclusive=True → 新消息自动取消旧 worker
  │  # group="chat"    → action_cancel_message() 查找并取消
  │
  ├── _tool_count = 0
  ├── _phase = "生成回复中..."
  │
  ├── 定义 _render_bar():                                # app.py:237
  │     if _tool_count > 0:
  │         bar.set_status(f"工具执行中 ({_tool_count})")
  │     else:
  │         bar.set_status(_phase)
  │
  ├── 定义 7 个流式回调 (闭包):
  │     │
  │     ├── _on_delta(token)                              # app.py:243
  │     │     ├── _phase = "生成回复中..."
  │     │     ├── _render_bar()
  │     │     └── stream.add_token(token)                 # widgets.py:160
  │     │           ├── _pending += token
  │     │           ├── if now - _last_update >= 0.08     # 节流 ~12 FPS
  │     │           │     or len(_pending) < 200:          # 初始快速显示
  │     │           │     self.content = _pending          # reactive → watch_content
  │     │           └── watch_content(content)             # widgets.py:169
  │     │                 └── self.update(Markdown(content))
  │     │
  │     ├── _on_thinking(token)                           # app.py:254
  │     │     ├── _phase = "思考中..."
  │     │     └── _render_bar()
  │     │
  │     ├── _on_thinking_done()                           # app.py:260
  │     │     ├── _phase = "生成回复中..."
  │     │     └── _render_bar()
  │     │
  │     ├── _on_tool_start(name, args_brief)              # app.py:265
  │     │     ├── _phase = "工具调用中..."
  │     │     └── _render_bar()
  │     │
  │     ├── _on_tool_exec_start(name, args, idx, total)   # app.py:273
  │     │     ├── _tool_count += 1
  │     │     ├── _render_bar()  # "工具执行中 (N)"
  │     │     └── stream.add_token(f"⚙ **{name}**({args})\n")
  │     │           └── stream._refresh()  ← 强制立即渲染
  │     │
  │     ├── _on_tool_exec_end(ev)                         # app.py:289
  │     │     ├── _tool_count -= 1
  │     │     ├── _render_bar()
  │     │     ├── if error: stream.add_token(f"✗ {detail}")
  │     │     └── stream._refresh()
  │     │
  │     └── _on_new_turn()                                # app.py:301
  │           ├── _phase = "生成回复中..."
  │           ├── _render_bar()
  │           └── stream.add_token("\n\n")
  │
  ├── try:
  │     result = await self._orche.process_message(       # app.py:308
  │         session_key, text, model=..., temperature=...,
  │         on_delta=_on_delta,
  │         on_thinking=_on_thinking,
  │         on_thinking_done=_on_thinking_done,
  │         on_tool_start=_on_tool_start,
  │         on_tool_execute_start=_on_tool_exec_start,
  │         on_tool_execute_end=_on_tool_exec_end,
  │         on_new_turn=_on_new_turn,
  │     )
  │     │
  │     └── Orchestrator.process_message()               # orchestrator.py:269
  │           ├── ctx.build_messages(...)                  # 上下文组装
  │           ├── Dispatcher.resolve() → Agent.run()       # 路由分发
  │           │     └── AgentCore.run()                    # runner.py:264
  │           │           └── LLM chat_stream → 逐 token 触发回调
  │           └── ctx.save_exchange(...)                   # 持久化
  │
  │   except asyncio.CancelledError:
  │       stream.add_token("\n\n*[cancelled]*\n")         # Escape 取消
  │       return
  │   except Exception as exc:
  │       chat.mount(ErrorMessage(str(exc)))              # app.py:332
  │       return
  │   finally:
  │       bar.hide()                                       # 折叠状态栏
  │       input_w.disabled = False                         # 恢复输入
  │       input_w.focus()
  │
  ├── stream.finish()                                     # app.py:339
  │     └── self.content = self._pending  ← 刷新所有 pending token
  │
  └── StatusFooter.set_usage(...)                         # app.py:342
        └── "Session: xxx  Tokens: N in / M out  3.2s  [react]"
```

### Worker 生命周期与取消

```
@work(exclusive=True, group="chat")
  │
  ├── 新消息到达 → Textual 自动取消旧 worker (exclusive=True)
  │     └── old worker 抛出 asyncio.CancelledError → "[cancelled]" 提示
  │
  ├── Escape 键 → action_cancel_message()                # app.py:165
  │     ├── 输入框有焦点 → 清空输入
  │     └── 否则 → 遍历 workers, 取消 group="chat" 的 worker
  │
  └── app.exit() → 所有 workers 随事件循环停止
```

### 消息渲染数据流

```
LLM token stream (SSE)
  │
  ├── content delta → _on_delta(token) → stream._pending += token
  │                                         │
  │                                         ├── [节流通过] stream.content = _pending
  │                                         │     └── reactive 触发 watch_content()
  │                                         │           └── self.update(Markdown(content))
  │                                         │                 └── Rich Markdown → Textual Widget
  │                                         │
  │                                         └── [0.15s 滚动节流] chat.scroll_end(animate=False)
  │
  ├── tool call → _on_tool_start() + _on_tool_exec_start()
  │                 └── stream._refresh()  ← 绕过节流，立即渲染
  │
  ├── tool result → _on_tool_exec_end()
  │                   └── stream._refresh()
  │
  └── stream.finish()
        └── stream.content = _pending  ← 最终提交
```

## 关键技术决策

| 决策 | 原因 | 替代方案 |
|------|------|---------|
| `@work(exclusive=True)` | 非阻塞消息处理 + 自动取消上一个 worker | `asyncio.create_task` + 手动 CancelToken |
| `reactive("")` | 声明式渲染，Textual 自动管理刷新和布局 | 手动 `self.update()` + `self.refresh()` |
| `DEFAULT_CSS` | 组件样式自包含，不依赖全局 CSS | 全部样式集中在 theme.css |
| 0.08s 节流 | 平衡实时性和性能（~12 FPS） | 逐 token 渲染（~100+ FPS，布局压力大） |
| 0.15s 滚动节流 | 避免高频 `scroll_end` 调用 | 每个 token 都滚动到底部 |
| 硬编码颜色 | Textual 不支持 `--custom-property` | `$token`（仅有预设值，不匹配 GitHub 配色） |
| `push_screen(callback)` | 非阻塞弹窗，callback 获取用户选择 | `push_screen_wait`（阻塞，不能用于事件处理器） |
| Mouse scroll → 聊天区 | 终端标准行为，滚轮应是滚动操作 | 禁用鼠标支持（会导致滚轮失效） |
| Shift+拖拽 → 文本选择 | 终端标准行为，Shift 绕过应用鼠标捕获 | `_disable_mouse_support()`（会导致 scroll 失效） |
