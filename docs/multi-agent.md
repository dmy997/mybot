# 多智能体范式 (Multi-Agent) — DeepResearch

## 概述

mybot 的多智能体能力**不是**一个特殊子系统，而是"又一个 agent 范式"——它内聚在一个
`BaseAgent` 子类的 `run()` 里，复用既有的自动发现（`discover_agents`）和四层路由
（`Dispatcher`）。ReAct 是"一个 AgentCore loop"，Plan-Solve 是"两个串联 loop"，
**DeepResearch 是"一个 lead loop + N 个并行 worker loop + 一个 synthesis loop"**。

因此新增多智能体**没有**改动 `Dispatcher`/`discover_agents`/`process_message`/
`AgentInput`/`AgentOutput` 任何核心契约——只加了一条 `/research` 路由。

## 三层解耦 + 一个薄范式

| 层 | 职责 | 位置 | 变化频率 |
|----|------|------|---------|
| **① Worker 运行时** | 跑一个隔离子代理（新建 `AgentCore` + 受限工具 + timeout），支持并行 fan-out | `agents/team/runner.py` | 几乎不变 |
| **② 协作拓扑** | lead 分解 → workers 并行 → synthesis 汇总；部分失败降级 | `agents/team/topology.py` | 加新拓扑时才变 |
| **③ 团队蓝图** | 声明式：角色 prompt、工具子集、模型、worker 数上限 | `agents/team/blueprint.py` + `prompt_templates/agent/deep_research/*.md` | **每个新应用一份** |
| **薄范式** | 绑定②+③，`paradigm="deep_research"`，被自动发现 | `agents/deep_research_agent.py` | 每应用一个薄类 |

> **DeepResearch = orchestrator-workers 拓扑 + deep_research 蓝图**。DeepResearch
> 本身不硬编进机制里，机制层对任何应用都通用。

## 文件结构

```
agents/team/               # 机制层（子包，NOT 自动发现——discover_agents 只扫 agents/*.py 顶层）
├── __init__.py            # 导出 SubAgentRunner / SubAgentSpec / SubAgentResult
├── runner.py              # SubAgentRunner: run() 单个 + run_all() 并行（信号量限流）
├── blueprint.py           # TeamBlueprint / WorkerRole（frozen dataclass，__post_init__ 校验）
└── topology.py            # OrchestratorWorkers.execute() + TeamResult

agents/deep_research_agent.py   # 薄范式（顶层→被发现），DEEP_RESEARCH 蓝图实例

prompt_templates/agent/deep_research/
├── lead.md                # 协调者：把主题分解成 JSON 子任务数组
├── worker.md              # 研究员：websearch + webfetch → 调研笔记
└── synthesize.md          # 主编：<summary>…</summary><report>…</report>
```

`tools/subagent.py`（`delegate` 工具）已重构为复用 `SubAgentRunner`——单一子代理运行时。

## 执行流程

```
DeepResearchAgent.run(spec)                       # agents/deep_research_agent.py
  ├─ _extract_topic(spec)                         # spec.goal 或末条 user 消息（剥掉 /research）
  ├─ SubAgentRunner(core.provider, workspace)
  └─ OrchestratorWorkers(core, runner).execute(topic, DEEP_RESEARCH, spec.tools)
        ├─ 1. _decompose  → lead loop（无工具）→ JSON 子任务数组（_parse_subtasks 容错）
        ├─ 2. fan-out     → 每子任务一个 worker（websearch+webfetch，allow_network）
        │                    runner.run_all(max_concurrent=3) 并行 + 信号量限流
        └─ 3. _synthesize → synthesis loop（无工具）→ <summary> + <report>
  ├─ _save_report(topic, full_report)             # {workspace}/research/{date}_{slug}.md
  └─ 返回 AgentOutput(content=摘要+文件路径, tool_events=worker 生命周期)
```

**报告推送策略**：全文写盘归档，只把**摘要 + 文件路径**作为流式回复——每周定时推送时，
避免往聊天糊一面墙的长文。worker 静默执行（不转发 token delta），但其生命周期以
`tool_events` 抛出，Web 端 Trace/Log 视图零改动即可渲染。

## 契约复用（零核心改动）

- **`BaseAgent.run(AgentInput) -> AgentOutput`** 不变；worker 派生所需的 `provider` 取
  `self.core.provider`，工具集取 `spec.tools`——都在现有契约内
- **worker 工具隔离**：`OrchestratorWorkers._select_tools` 剔除 `delegate`（防递归），
  `SubAgentRunner._build_tools` 再套 `subagent`-scope 的 `ToolGuard`
- **优雅降级**：单 worker 超时/异常 → `SubAgentResult(success=False)`，synthesis 用剩余
  结果继续，失败进 `worker_results`，不整体 fail

## 路由与周期触发

`/research` 注册在 `Dispatcher._EXPLICIT_ROUTES`（第 1 层，零开销确定性命中）：

```python
(re.compile(r"^/research\b", re.IGNORECASE), "deep_research")
```

**每周 DeepResearch**（复用[统一定时任务框架](scheduled-tasks.md)，零新调度代码）：

```
用户: "每周一早九点研究 AI Agent 前沿进展并推给我"
  → agent 调用 schedule_task(action="create",
        cron="0 9 * * 1", task="/research AI Agent 前沿进展")
    → 建 user push 任务（channel=当前渠道）

（每周一 09:00）
CronScheduler → ScheduledTaskService.fire("user:xxxx")
  → deliver 回调注入 bus → process_message(source="cron")
    → "/research …" 命中 deep_research 范式
      → 团队跑完 → 摘要+报告路径经 deliver 管道推回聊天
```

## 扩展未来场景

| 场景 | 需要做什么 | 动核心吗 |
|------|-----------|---------|
| 换研究主题 | 换 `/research` 后的主题文本 | 否 |
| 新应用、同拓扑（如"选题委员会"） | 新蓝图③ + ~15 行薄范式 | 否（自动发现） |
| 新拓扑（pipeline / debate / 分层） | 新 `agents/team/*.py` + 薄范式 | 否 |
| 纯配置驱动（无代码新应用） | 再加一个"通用 multi_agent 范式"读运行时蓝图 | 否（三层共享，零返工） |

## 护栏

- **成本/限流**：`TeamBlueprint.max_workers`（默认 5）+ `max_concurrent`（默认 3 信号量）；
  worker 可用便宜模型（`WorkerRole.model`），synthesis 用强模型（`synthesis_model`）
- **超时**：per-worker `timeout_seconds`（默认 180s），`asyncio.wait_for` 保护
- **递归防护**：workers 不带 `delegate`、不带拓扑范式 → 不会无限嵌套

## 相关文档

- [统一定时任务框架](scheduled-tasks.md) — 周期触发 DeepResearch 的调度层
- [流式输出与路由](streaming-and-routing.md) — Dispatcher 四层路由 / deliver 推送管道
