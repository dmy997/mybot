# 小红书海龟汤自动运营 (Xiaohongshu Turtle Soup)

## 概述

mybot 通过 **Skill + Tool + 定时任务** 三层架构实现小红书海龟汤内容的自动生成和发布：

- **定时任务** — 作为一个**系统级 scheduled task**（`system:xiaohongshu`）注册到统一的
  `ScheduledTaskService`，每天 20:00（cron `0 20 * * *`）触发一次
- **Skill**（`skills/xiaohongshu/SKILL.md`）提供完整的海龟汤运营知识和工作流
- **Tool**（`tools/xiaohongshu_publish.py`）提供浏览器自动化发布能力
- **Agent** 加载 skill、调用工具，执行全链路操作

> 海龟汤发帖过去用独立的 `XiaohongshuService` + `interval_hours=24`（会随进程启动时间漂移）。
> 现在它是[统一定时任务框架](scheduled-tasks.md)里的一个 `system` 任务——固定在墙钟 20:00 触发，
> `channel=None` 表示"内部副作用任务"（无需推送给用户，agent 通过工具产生效果）。

## 架构概览

```
CronScheduler (cron "0 20 * * *")
  → Orchestrator._on_cron_job("system:xiaohongshu")
    → ScheduledTaskService.fire("system:xiaohongshu")
      → run_agent 回调 = Orchestrator._run_scheduled_agent(...)
        → orchestrator.process_message(
              session_key="xiaohongshu",
              user_input=xiaohongshu_prompt(workspace),
              skills=["xiaohongshu"],
              source="cron",
            )
          → Agent loads SKILL.md into system prompt
            → Read state.json → decide phase (tangmian/tangdi)
              → Generate content (LLM, following skill instructions)
                → Write output file
                  → Call xiaohongshu_publish tool
                    → scripts/xhs_publish.py → Playwright → 小红书
```

**关键设计决策**：定时层不做任何实际工作——只在到点时把一条 prompt 注入 agent。Agent 拥有完整的上下文（skill 指令 + 工具能力 + LLM 能力），负责从状态检查到最终发布的全链路。系统任务用 `system=True` 保护，用户无法通过聊天取消。

## 文件结构

| 文件 | 角色 |
|------|------|
| `skills/xiaohongshu/SKILL.md` | 海龟汤运营知识和工作流（Agent 的"大脑"） |
| `tools/xiaohongshu_publish.py` | 小红书发布 Tool（Agent 的"手"） |
| `scripts/xhs_publish.py` | Playwright 浏览器自动化脚本；**无图时自动把文本渲染成封面卡片** |
| `scripts/xhs_text_image.py` | 用 Pillow + Noto CJK 字体把 title/content 渲染成 1080×1440 图文卡片 |
| `services/xiaohongshu.py` | 仅保留 `XIAOHONGSHU_SESSION_KEY` 常量 + `xiaohongshu_prompt(workspace)`（构造触发指令） |
| `services/scheduled_tasks.py` | 统一定时任务框架——`seed_system_task` 注册海龟汤系统任务 |
| `core/orchestrator.py` | 启动时 seed 系统任务 + `_on_cron_job` 路由到 `_scheduled.fire` |

### 工作目录

```
workspace/xiaohongshu/
├── state.json                  # Agent 管理：{phase, series, last_tangmian_content}
├── 20260630_tangmian.md       # 第 N 期汤面
├── 20260701_tangdi.md         # 第 N 期汤底
└── ...
```

## Skill 工作流

`SKILL.md` 定义了 agent 的完整行为：

1. **读取状态** — `read` 工具读 `state.json`，获取 phase、series、last_tangmian_content
2. **生成汤面** — 创作 200-500 字谜题，小红书排版，结尾"答案明天揭晓"
3. **生成汤底** — 读取上次汤面，写出 300-600 字解答，逐步推理
4. **发布** — 调用 `xiaohongshu_publish` tool，传入 title + content
5. **更新状态** — `write` 工具原子更新 state.json，切换 phase

Agent 通过 skill 指令自然知道该做什么——无需硬编码工作流。

## 发布 Tool

`XiaohongshuPublishTool`（`tools/xiaohongshu_publish.py`）：

- `name = "xiaohongshu_publish"`
- 参数：`title`（≤20 字）、`content`（谜题正文，渲染到封面图）、`caption`（可选，文本框内容——引导语 + 标签）、`images`（可选图片列表）
- 实现：通过 `subprocess` 调用 `scripts/xhs_publish.py`
- **图文分离**：`content` 是谜题本身，被渲染到封面卡片；`caption` 是"答案明天揭晓 + #标签"等与谜题无关的引导文案，只填入笔记文本框，不出现在图片上（`caption` 为空时回退用 `content` 填文本框）
- **自动封面卡**：未传 `images` 时，`xhs_publish.py` 会用 Pillow 把标题+`content` 渲染到
  1080×1440 的暖色卡片上（`scripts/xhs_text_image.py`）作为封面图发布。小红书图文笔记
  必须先上传图片才会出现标题/正文输入框，所以纯文本发帖也必须有至少一张图
- `capabilities = {NETWORK}`，`_scopes = {"core"}`

### 首次使用：获取 Cookie

```bash
pip install playwright && playwright install chromium
python scripts/xhs_publish.py --login
# 浏览器弹出 → 扫码登录小红书创作中心 → 按 Enter 保存 cookie
```

Cookie 保存到 `scripts/xhs_cookies.json`，后续发布自动复用。

### 手动测试发布

全自动（无头）模式通过 `add_init_script` 注入 `Element.prototype.attachShadow` 补丁
强制打开 closed shadow root，再通过 `xhs-publish-btn >> text=发布` 定位并点击真实的
发布按钮。发布后到「笔记管理」页面校验确认。

```bash
# 全自动（需要 playwright + cookie 已就绪）
python scripts/xhs_publish.py --payload '{"title":"T","content":"C","caption":"CTA #tag"}'

# 有头辅助（填好后人工点发布，再回终端 Enter 校验）
python scripts/xhs_publish.py --assist --title "🐢 海龟汤" --content "谜题正文..." --caption "..."
```

### 发布未确认 → 微信文件助手回退

全自动无头模式**可能填好笔记却点不动**小红书那个 closed-shadow 的 `<xhs-publish-btn>` 发布按钮；
此时 `_verify_published` 在「笔记管理」列表查不到标题，脚本返回 `status: "unconfirmed"` 并以退出码 2 结束
（`scripts/xhs_publish.py` 会把封面图路径 `image` + `caption` 一并写进 stdout 的 JSON）。

为避免定时任务在无人值守时静默丢稿，`XiaohongshuPublishTool` 支持 `set_notify(cb)` 注入一个回调：
未确认时工具解析 stdout 里的 `image` + 文案，调 `cb({title, content, caption, image})`。WeChat 渠道
（`channels/wechat.py`）在 `start()` 时把 `_notify_publish_fallback` 接上——用 itchat 把**文案 + 封面图**
发到微信**文件传输助手**（`filehelper`，可用 `XIAOHONGSHU_FALLBACK_CHAT` 覆盖），你在手机上一键手动发布即可。

此时工具仍返回 `success=False`，agent 按规则**不推进状态机**（不会把没发出去的稿误标为已发）。

> HTTP/SSE 入口没有常驻自推送渠道，未接 `set_notify`，行为保持原样（仅返回错误）。

## 手动触发 cron 任务

```python
# 在 mybot-server 运行时的另一个终端
await orchestrator.cron.run_job_now("system:xiaohongshu")
```

## 代码调用链

### 完整请求流（cron 触发 → 发布成功）

```
CronScheduler._on_timer()                             # services/cron.py
  └─ Orchestrator._on_cron_job("system:xiaohongshu")  # core/orchestrator.py
      └─ ScheduledTaskService.fire("system:xiaohongshu")  # services/scheduled_tasks.py
          └─ Orchestrator._run_scheduled_agent(...)   # run_agent 回调
              └─ orchestrator.process_message(         # core/orchestrator.py
                   session_key="xiaohongshu",
                   user_input=xiaohongshu_prompt(workspace),
                   skills=["xiaohongshu"],
                   source="cron",
                 )
              ├─ ContextManager.build_messages()      # context/context_manager.py:305
              │   ├─ 修复中断 session
              │   ├─ _build_system_prompt()           # context/context_manager.py:553
              │   │   └─ _build_static_prompt()       # context/context_manager.py:641
              │   │       └─ SkillsLoader.get("xiaohongshu")
              │   │           └─ 注入 skills/xiaohongshu/SKILL.md → system prompt
              │   ├─ 加载 session history (cursor-based, 100-msg cap)
              │   └─ token-budget 检查 → 必要时压缩
              ├─ Dispatcher.resolve(user_input)       # core/dispatcher.py:155
              │   └─ 四层路由 → "react" 或 "plan_solve"
              ├─ Agent.run(AgentInput)                 # core/runner.py:263
              │   └─ AgentCore.run()                   # core/runner.py:263
              │       └─ loop:
              │           ├─ _compact_for_llm()        # core/runner.py:614
              │           ├─ _call_llm() → provider.chat_with_retry/stream
              │           │   # Agent 按 SKILL.md 指令: read state.json, 判断 phase
              │           ├─ LLM 返回 tool_calls:
              │           │   ├─ read: workspace/xiaohongshu/state.json
              │           │   ├─ write: 输出文件 (tangmian.md / tangdi.md)
              │           │   └─ write: 更新 state.json (切换 phase)
              │           └─ 最终 tool_call: xiaohongshu_publish
              │               └─ _execute_tool_calls() # core/runner.py:988
              │                   └─ XiaohongshuPublishTool.execute()
              │                       └─ tools/xiaohongshu_publish.py
              │                           ├─ 验证 title (≤20字) + content
              │                           ├─ 构建 JSON payload
              │                           └─ asyncio.create_subprocess_exec(
              │                               scripts/xhs_publish.py --payload {...}
              │                             )
              │                               └─ Playwright 浏览器自动化
              │                                   ├─ 加载 cookies (xhs_cookies.json)
              │                                   ├─ goto 创作中心发布页
              │                                   ├─ fill 标题 + 正文
              │                                   ├─ click 发布按钮
              │                                   └─ 返回 {status, note_id, url}
              ├─ ctx.save_exchange()                  # 保存到 session
              └─ Consolidator.maybe_consolidate()     # fire-and-forget 压缩
```

### 手动发布路径

```
用户输入 → Orchestrator.run() CLI                      # core/orchestrator.py
  → process_message(skills=["xiaohongshu"], user_input="发布...")
    → [同上 Agent 执行流程]
```

## 错误处理

| 场景 | 行为 |
|------|------|
| Cookie 过期 | Tool 返回失败，agent 告知用户，状态不更新。重新 `--login` |
| Playwright 未安装 | Tool 返回清晰错误信息，附带安装命令 |
| LLM 生成失败 | Agent 重试，最终告知用户 |
| 发布失败 | 状态不更新，下次 cron 触发时重试当前阶段 |
| state.json 损坏 | Agent 默认 phase=tangmian, series=1 |
