# Claude Code CLI 流式输出方案分析 & mybot 重构计划

## 一、Claude Code 的流式渲染架构

### 核心技术栈

Claude Code 使用 **自定义 Ink（React for Terminal）** 渲染框架，约 25 万行 TypeScript：

```
React 组件树 → Yoga Flexbox 布局 → renderNodeToOutput（DOM 遍历 + 滚动逻辑）
→ Output 操作收集 → Screen Buffer（双缓冲 cell 数组）
→ diffEach（逐 cell 对比新旧帧）→ ANSI escape 补丁 → stdout
```

### 关键设计点

#### 1. 双缓冲 + Cell 差分渲染

`/src/ink/screen.ts` — 维护一个 `Uint32Array` 的 cell 缓冲区，每帧计算新旧差异，只输出变化的 cell 对应的 ANSI 序列。**使用绝对光标定位**（`CSI row;col H`），不存在 Rich Live 的 `cursor_up` 逃逸码漂移问题。

#### 2. ScrollBox 自动跟随

`/src/ink/render-node-to-output.ts:745-795` — 每帧检查：

```
if was_at_bottom OR sticky_scroll:
    scrollTop = maxScroll  # 钉到底部
```

这是解决"内容超出终端高度后滚动冻结"问题的核心机制：**每帧都重新计算并钉住滚动位置**，而不是依赖光标定位。

#### 3. 交替屏幕模式

`/src/ink/components/AlternateScreen.tsx` — 主流使用 `\x1b[?1049h` 切换到交替屏幕，拥有完整终端控制权。非全屏模式（LogUpdate）仅在降级场景使用。

#### 4. 流式状态管理

流式数据通过 React 状态流转：

```
LLM stream → handleMessageFromStream() → 6 个回调
→ messages[] + streamingText + streamingToolUses + streamingThinking
→ useDeferredValue (transition priority) → <Messages> 组件树
→ Ink reconciler → Yoga Layout → 渲染到终端
```

关键：`streamingText` 累积原始 token，`visibleStreamingText` 只取最后一行（避免逐字符渲染导致的抖动），工具调用有独立的 `streamingToolUses` 状态数组。

#### 5. Per-Turn 重置

每个 LLM turn 开始前：清空 `streamingToolUses`、`streamingText`、`streamingThinking`。消息历史 `messages[]` 保持累积。

---

## 二、mybot 现有方案 vs Claude Code 对比

| 维度 | Claude Code | mybot (当前) |
|------|-----------|------------|
| **渲染框架** | 自定义 Ink (React → Terminal) | 直接 `console.file.write()` |
| **光标定位** | 绝对定位 (CSI r;c H) | 无（依赖终端原生流） |
| **滚动方案** | ScrollBox 每帧重算 scrollTop | 终端原生滚动 |
| **屏幕模式** | 交替屏幕为主 | 主屏幕（直接写入） |
| **帧差渲染** | ✅ cell 级 diff，只输出变化 | ❌ 全量写入 |
| **Markdown** | 自定义 Markdown 组件 (marked lexer) | 无（流式期间原始文本） |
| **工具调用 UI** | 独立组件：名称+spinner+进度 | Rich Status spinner + console.print |
| **多 turn 处理** | messages[] 累积 + streaming 重置 | buffer 累积 + on_new_turn 清空 |
| **代码量** | ~25 万行 TypeScript | ~170 行 Python |
| **平台** | Node.js | Python |

---

## 三、Claude Code 方案中可移植的概念

### 可以直接采纳的设计思路

1. **ScrollBox 自动跟随算法** — 不依赖光标定位，每帧显式计算 scrollTop。可提炼为纯 Python 类，在任何 TUI 方案中复用。

2. **交替屏幕 + onEnd 输出到主屏幕** — 流式期间用交替屏幕确保滚动正确，完成后把最终内容 print 到主屏幕保留在 scrollback 中。

3. **工具调用独立渲染通道** — 工具调用不经过流式渲染区域，直接输出到屏幕（在交替屏幕中就是直接 print），避免与流式内容的光标定位冲突。

4. **逐行输出而非全量重渲染** — 不使用 Live 重渲染整个 buffer，每次只输出新增 token（自然滚动）。

### 不能直接移植的部分

- **自定义 Ink 框架** — 25 万行代码，TypeScript → Python 不可行
- **React 生态** — Python 没有 React，没有同等成熟的 terminal reconciler
- **Yoga Flexbox 布局** — 终端布局无需 CSS Flexbox

---

## 四、重构方案

### 方案 A：使用 Textual（Python TUI 框架）— 推荐

**Textual** 是 Python 生态中最接近 Claude Code Ink 的方案：
- 内置 ScrollView 组件，自动处理滚动跟随
- 基于 Rich（mybot 已使用），Markdown 渲染天然兼容
- Widget 系统类似于 React 组件模型
- 内置 CSS 布局、键盘/鼠标事件、终端 resize 处理
- 成熟度：Textualize 团队维护，社区活跃

**重构范围**：

```
observability/stream_renderer.py  →  废弃，由 Textual widget 替代

新增: mybot/tui/                   →  Textual 界面模块
  ├── app.py                       →  Textual App 入口
  ├── chat_view.py                 →  聊天消息列表 (ScrollView)
  ├── input_bar.py                 →  输入区域 + 发送按钮
  ├── message_widget.py            →  单条消息渲染 (Markdown)
  ├── tool_call_widget.py          →  工具调用展开/折叠
  └── theme.css                    →  Textual CSS 主题
```

**优点**：
- 彻底解决滚动问题（ScrollView 使用绝对光标定位）
- 工具调用可见（独立的 widget 区域，不被交替屏幕隐藏）
- 可扩展的 UI（侧边栏、状态栏、多面板等）
- 代码结构清晰，widget 树类似于 React 组件树
- 支持鼠标、键盘快捷键、终端 resize

**缺点**：
- 引入新依赖（`textual`）
- CLI 界面完全重写（~2-3 天工作量）
- Textual 学习曲线
- 不能复用现有 `StreamRenderer` 代码

**工作量估算**：约 800-1200 行新代码，删除 ~200 行旧代码

---

### 方案 B：使用 prompt_toolkit 全屏应用 + Rich Markdown

prompt_toolkit 已是 mybot 的依赖（用于 CLI 输入），其 `Application` 类支持：
- 全屏布局（HSplit/VSplit/Window）
- Window 级别的滚动
- 键盘/鼠标事件

**重构范围**：

```
observability/stream_renderer.py  →  改为 prompt_toolkit Window
                                    token 写入 Buffer → 自动滚动

observability/display.py          →  改为 prompt_toolkit 输出方式

core/orchestrator.py              →  main() 改为 Application.run()
```

**优点**：
- **不需要新依赖**（prompt_toolkit 已安装）
- Window 滚动比 Rich Live 可靠
- 与现有输入代码（PromptSession）一致

**缺点**：
- prompt_toolkit 的布局系统不如 Textual 成熟
- Markdown 渲染需要手动集成（prompt_toolkit 没有内置 Markdown 支持）
- 文档和社区不如 Textual 活跃
- Window 滚动在超长内容时仍有边界情况

**工作量估算**：约 500-800 行新代码

---

### 方案 C：保留 Rich，用交替屏幕 + 手动滚动管理

在现有 Rich 基础上，参考 Claude Code 的 ScrollBox 思路：

1. 使用 Rich `Live(screen=True)` 进入交替屏幕
2. 在交替屏幕中手动管理"可见行范围"（scrollTop）
3. 维护 Markdown 渲染后的行数组
4. 每帧只渲染可见行范围内的内容（viewport culling）
5. 新内容到达时自动调整 scrollTop 钉到底部
6. 工具调用直接 `console.print()` 到交替屏幕（不会被隐藏）

```
新增: observability/scroll_tracker.py  →  ScrollTracker 纯逻辑类
修改: observability/stream_renderer.py →  行缓冲 + 视口裁剪 + screen=True
```

**优点**：
- **不需要新依赖**，基于现有 Rich
- 改动量最小（~300 行新代码）
- 保留了 Markdown 渐进渲染

**缺点**：
- Rich Live 的 `screen=True` 仍会隐藏工具输出（需要手动 pause/print/resume）
- Rich 不是为全屏滚动容器设计的，可能遇到边缘情况
- 需要自己实现行级别的差分渲染（避免全量重绘闪烁）
- 不如 Textual 或 prompt_toolkit 的方案成熟

**工作量估算**：约 300-500 行新代码/修改

---

### 方案 D：极简方案 — 放弃渐进式 Markdown，只用直接输出

当前已实现的方案（5fadf12 commit）：直接 `console.file.write(delta)` + 终端原生滚动。

**优化空间**：
- 在 `on_end()` 时清除原始 token 行，用 Rich Markdown 重新渲染（需要光标定位）
- 或者在 completion 后单独一行输出渲染后的 Markdown（不覆盖原始内容）

**优点**：已经实现，零额外工作

**缺点**：流式期间看到 Markdown 语法，体验较差

---

## 五、推荐方案及实施计划

### 推荐：方案 A（Textual）

理由：
1. Textual 是解决"终端滚动容器"问题的最成熟 Python 方案
2. 与 Rich 兼容（Textual 基于 Rich），Markdown 渲染可复用
3. 架构清晰，后期可扩展（side panel、status bar、多 tab 等）
4. 一次性解决，不需要后续反复修补

### 实施计划

#### Phase 1：Textual 基础框架（预计 4-6h）

| 步骤 | 文件 | 内容 |
|------|------|------|
| 1.1 | `mybot/tui/__init__.py` | 模块初始化 |
| 1.2 | `mybot/tui/app.py` | Textual `App` 子类，挂载 widget 树，绑定 `Orchestrator` |
| 1.3 | `mybot/tui/theme.css` | 暗色主题 CSS（从现有 `server_web/index.html` CSS 变量移植） |
| 1.4 | `mybot/tui/chat_view.py` | `ScrollView` 子类，消息列表容器，自动跟随滚动 |
| 1.5 | `mybot/tui/input_bar.py` | `Input` widget，处理用户提交 |

#### Phase 2：消息渲染组件（预计 3-4h）

| 步骤 | 文件 | 内容 |
|------|------|------|
| 2.1 | `mybot/tui/message_widget.py` | 单条消息 Markdown 渲染组件 |
| 2.2 | `mybot/tui/tool_call_widget.py` | 工具调用折叠卡片（名称 + 状态 + 展开参数） |
| 2.3 | `mybot/tui/spinner_widget.py` | thinking 状态指示器 |

#### Phase 3：流式输出集成（预计 3-4h）

| 步骤 | 文件 | 内容 |
|------|------|------|
| 3.1 | `core/orchestrator.py` | CLI `main()` 改为 Textual App 入口，保留 `process_message()` 调用 |
| 3.2 | `observability/stream_renderer.py` | 废弃，由 Textual widget 替代 |
| 3.3 | `observability/display.py` | 工具进度打印改为向 Textual widget 推送事件 |

#### Phase 4：测试与清理（预计 2-3h）

| 步骤 | 内容 |
|------|------|
| 4.1 | 新增 `test/tui/` 测试目录（Textual 有内置测试工具） |
| 4.2 | 删除旧的 `StreamRenderer` 相关测试引用 |
| 4.3 | 端到端手动测试：长文本流式、多轮对话、工具调用、Ctrl+C 中断 |

### 备选：如果不想引入新依赖

选择 **方案 B（prompt_toolkit）**，Phase 1-3 改为：
- 使用 `prompt_toolkit.layout.Layout` 替代 Textual App
- 使用 `Window` + `Buffer` 替代 ScrollView
- 手动管理 Markdown → 格式化文本的转换

---

## 六、Claude Code ScrollBox 核心算法的 Python 实现

无论选哪个方案，Claude Code 的 ScrollBox 自动跟随逻辑都值得单独提炼为通用组件：

```python
class ScrollTracker:
    """Claude Code ScrollBox 风格的滚动追踪器。

    每帧调用 update()，传入内容高度和视口高度。
    若用户之前在底部（或 sticky=True），自动钉到底部。

    核心逻辑等价于 Claude Code render-node-to-output.ts:757-768
    """

    def __init__(self):
        self.scroll_top = 0
        self.prev_content_height = 0
        self.sticky = True  # 默认跟随

    def scroll_to(self, y: int) -> None:
        """主动滚动到指定位置，断开 sticky。"""
        self.scroll_top = max(0, y)
        self.sticky = False

    def scroll_to_bottom(self) -> None:
        """滚动到底部，恢复 sticky。"""
        self.sticky = True

    def scroll_by(self, dy: int) -> None:
        """相对滚动。"""
        self.scroll_top = max(0, self.scroll_top + dy)
        self.sticky = False

    def update(self, content_height: int, viewport_height: int) -> int:
        """计算新的 scroll_top。返回滚动位置变化量（delta）。

        当内容增长时，若之前已滚动到底部，自动跟随。
        """
        max_scroll = max(0, content_height - viewport_height)
        prev_max_scroll = max(0, self.prev_content_height - viewport_height)
        grew = content_height >= self.prev_content_height
        was_at_bottom = self.scroll_top >= prev_max_scroll

        if self.sticky or (grew and was_at_bottom):
            old_scroll_top = self.scroll_top
            self.scroll_top = max_scroll
            self.prev_content_height = content_height
            return self.scroll_top - old_scroll_top

        self.scroll_top = min(self.scroll_top, max_scroll)
        self.prev_content_height = content_height
        return 0

    @property
    def visible_range(self, viewport_height: int) -> tuple[int, int]:
        """返回可见行范围（start 包含，end 不包含）。"""
        return (self.scroll_top, self.scroll_top + viewport_height)
```

无论选择方案 A（Textual）、方案 B（prompt_toolkit）、还是方案 C（Rich），这个 `ScrollTracker` 都可以复用。Textual 的 ScrollView 内置了等价的逻辑，不需要手动实现。

---

## 七、决策矩阵

| 因素 | 方案 A Textual | 方案 B prompt_toolkit | 方案 C Rich 改进 | 方案 D 直接输出 |
|------|:--:|:--:|:--:|:--:|
| 滚动可靠性 | ★★★★★ | ★★★★ | ★★★ | ★★ |
| 新依赖 | textual | 无 | 无 | 无 |
| 工作量 | 大 | 中 | 小 | 已实现 |
| Markdown 渐进渲染 | ✅ | 需手动实现 | ✅ | ❌ |
| 工具调用可见性 | ✅ | ✅ | ⚠️ 需 pause | ✅ |
| 可扩展性 | ★★★★★ | ★★★ | ★★ | ★ |
| 代码结构清晰度 | ★★★★★ | ★★★ | ★★ | ★★ |

---

## 八、结论

Claude Code 的流式渲染方案**核心思路可以借鉴**，但代码不能直接移植（TypeScript → Python，自定义 Ink 框架 25 万行）。

**最值得采纳的设计**：
1. **ScrollBox 自动跟随算法**（每帧显式计算 scrollTop，不依赖光标相对定位）
2. **双缓冲帧差渲染**（只输出变化的 ANSI 序列，使用绝对光标定位）
3. **工具调用独立渲染通道**（不干扰流式文本区域的光标状态）

**推荐方案 A（Textual）**，预计 12-16h 完成全部重构，输出一个不亚于 Claude Code 终端体验的 CLI 聊天界面。

---

## 九、实际实施状态（2026-07）

TUI 已于 2026-06-28 实现，与方案 A 计划存在以下差异：

### 实际文件结构

```
tui/                              # 注: 顶层 tui/ 而非 mybot/tui/
├── __init__.py
├── app.py                        # Textual App 入口 (~471 行)
├── widgets.py                    # 消息 + 工具调用 widget (~354 行)
├── screens.py                    # 屏幕布局 (~107 行)
└── theme.css                     # 暗色主题 (~72 行)
```

### 与计划的差异

| 维度 | 计划 (方案 A) | 实际实施 |
|------|-------------|---------|
| 包路径 | `mybot/tui/` | `tui/`（顶层） |
| 文件拆分 | 5 个 widget 文件 | 3 个文件（widgets.py 合并了 message + tool_call + spinner） |
| 代码量 | 预计 800-1200 行 | 实际 ~1000 行 |
| stream_renderer.py | 计划废弃 | **仍在使用**（173 行，observability/ 下） |
| 流式方案 | Textual widget 替代 | Textual widget + Rich console.file.write 共存 |

### stream_renderer.py 状态

`observability/stream_renderer.py` 未被废弃——它与 Textual TUI 共存：
- Textual TUI 处理 CLI 交互式聊天界面
- `StreamRenderer` 继续为 `console.file.write()` 路径提供格式化输出
- 两者服务于不同场景：TUI 用于交互，StreamRenderer 用于简单输出

### 未实施的计划项

- `observability/scroll_tracker.py` — 未创建，Textual ScrollView 内置了滚动管理
- 交替屏幕 (方案 C) — Textual 使用自己的屏幕管理系统
- prompt_toolkit 方案 B — 仅保留用于 CLI 输入（`PromptSession`），未扩展到全屏渲染

此文档的第五、六、七节保留作为历史参考，展示了设计决策过程。
