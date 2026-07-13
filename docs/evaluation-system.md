# 评估系统 (Evaluation System)

## 概述

mybot 的评估系统采用 **两层架构**：Layer 1 为自定义 YAML 任务评估（规则评分、CI 集成），Layer 2 为社区基准集成（BFCL、GAIA）。参考 hello-agents 的 `Dataset → Evaluator → Metrics` 模式，但针对 mybot 的 AgentCore API 做了适配。

## 两层架构

```
Layer 1: 自定义任务评估                     Layer 2: 社区基准集成
  YAML 任务定义                              ┌─ BFCL (函数调用准确率)
       ↓                                    │    AST 匹配 + 6 个分类
  AgentCore 执行                              │
       ↓                                    └─ GAIA (通用助手能力)
  规则评分器 (4 个维度)                        准精确匹配 + 3 个难度级别
       ↓
  Terminal / Markdown / JSON 报告
```

## 快速开始

```bash
# Layer 1 — 自定义任务
python -m evals                                    # 所有任务 (react 范式)
python -m evals --paradigm plan_solve              # 单范式
python -m evals --paradigm react --paradigm plan_solve  # 对比两种范式
python -m evals --task file_read_basic             # 单个任务
python -m evals --output report.md                 # 输出 Markdown 报告
python -m evals --json results.json                # 输出 JSON

# Layer 2 — 社区基准
python -m evals --benchmark bfcl --category simple_python --max-samples 20
python -m evals --benchmark gaia --level 1

# CI 模式 (pytest, 不调用 LLM)
pytest evals/ -v
```

## 一、Layer 1 — 自定义任务评估

### 1.1 任务定义 (YAML)

任务文件放在 `evals/tasks/{category}/` 下，每个 YAML 文件定义一个任务：

```yaml
id: file_read_basic
description: "Find and read a specific source file"
prompt: "Find the file in this project that contains the class 'TokenBudget'..."
expected_tools: [grep, Read]           # 期望使用的工具
expected_in_answer:                    # 期望答案中包含的关键词
  - effective_window
  - warning_threshold
max_steps: 8                           # 最大步数（效率评分用）
timeout_seconds: 120                   # 超时时间
```

**字段说明**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | str | 任务唯一标识 |
| `description` | str | 任务描述 |
| `prompt` | str | 发送给 Agent 的用户消息 |
| `expected_tools` | list[str] | 期望使用的工具名称列表 |
| `expected_in_answer` | list[str] | 期望在回答中出现的关键词 |
| `max_steps` | int | 最大步数（默认 10） |
| `timeout_seconds` | int | 超时秒数（默认 120） |

现有 9 个任务，分 3 个类别：

| 类别 | 任务数 | 验证目标 |
|------|--------|----------|
| `tool_use` | 3 | 基础工具使用、多工具串联、grep 搜索 |
| `reasoning` | 3 | 代码分析、范式对比、多步推理 |
| `robustness` | 3 | 多文件查询、空搜索结果、文件不存在 |

### 1.2 执行流程

```
discover_tasks() → _load_task(YAML) → EvalTask
    ↓
_run_agent()
    ├─ ToolRegistry.register(tools)
    ├─ AgentCore(provider, workspace)
    ├─ AgentInput(init_messages, tools=registry, ...)
    ├─ await core.run(spec)
    └─ return {content, tools_used, tool_events, step_count, error}
    ↓
CompositeScorer.score(task._raw, output) → [ScoreResult...]
    ↓
compute_overall(scores) → EvalResult
```

### 1.3 评分器

所有评分器继承 `Scorer` 抽象基类，输出 `ScoreResult(name, value=0.0-1.0, passed, detail)`：

| 评分器 | 权重 | 算法 |
|--------|------|------|
| `CompletionScorer` | 1.0 | 无 error → 1.0，有 error → 0.0 |
| `KeywordScorer` | 1.0 | hits/expected 比例，≥0.6 通过 |
| `ToolSetScorer` | 1.0 | Jaccard 相似度（交集/并集），≥0.5 通过 |
| `StepEfficiencyScorer` | 0.5 | 1 − steps/max_steps |

`CompositeScorer` 组合多个评分器，按权重计算加权平均。默认评分器为 `CompletionScorer`、`KeywordScorer`、`ToolSetScorer`、`StepEfficiencyScorer`，权重分别为 [1.0, 1.0, 1.0, 0.5]。

### 1.4 pytest 集成

`evals/conftest.py` 提供 `--live-eval` 选项。CI 模式（默认）不调用 LLM，只验证任务加载、数据结构、评分管线：

```bash
pytest evals/ -v                          # CI 模式：20 个测试
pytest evals/ -v --live-eval              # 需要真实 LLM
```

## 二、Layer 2 — 社区基准集成

### 2.1 BFCL (Berkeley Function Calling Leaderboard)

**数据源**：[gorilla 仓库](https://github.com/ShishirPatil/gorilla) `bfcl_eval/data/`

**评价方法**：AST 匹配。将预测的函数调用和 ground truth 分别转为 hashable tuple（`_to_ast_key`），做集合比较。

```
BFCLLoader(data_dir)
    ├─ load(category, max_samples) → [{id, question, functions, ground_truth}]
    └─ load_ground_truth(category) → possible_answer/*.json

BFCLEvaluator(data_dir)
    └─ evaluate(agent_factory, category, max_samples)
         ├─ _extract_calls(response)     # JSON 数组 / 代码块 / 正则回退
         ├─ _ast_match(predicted, truth)  # 集合比较 AST keys
         └─ return {results, metrics}

BFCLMetrics
    └─ compute(results) → {overall_accuracy, category_accuracy, error_rate}
```

**支持 6 个分类**：`simple_python`, `simple_java`, `simple_javascript`, `multiple`, `parallel`, `irrelevance`

### 2.2 GAIA (General AI Assistants)

**数据源**：HuggingFace gated dataset `gaia-benchmark/GAIA`（需申请访问，设置 `HF_TOKEN`）

**依赖**：`pip install huggingface-hub pyarrow`（或 `pip install -e ".[evals]"`）

**评价方法**：准精确匹配（quasi-exact match）。对预测答案和预期答案做规范化后比较。

**数据格式**：元数据优先从 `metadata.parquet`（当前格式）加载，回退到 `metadata.jsonl`（旧格式）。

```
GAIALoader(local_data_dir, split)
    └─ load(level, max_samples) → [{task_id, question, final_answer, level}]
        ├─ _load_from_parquet()   # pyarrow 读取 parquet
        └─ _load_from_jsonl()     # 回退：逐行 JSON

GAIAEvaluator(data_dir, split)
    └─ evaluate(agent_factory, level, max_samples)
         ├─ _extract_answer(response)        # FINAL ANSWER: 格式提取
         ├─ _quasi_exact_match(pred, exp)     # 规范后精确 + 子串
         └─ return {results, metrics}

GAIAMetrics
    └─ compute(results) → {exact_match_rate, partial_match_rate, level_accuracy}
```

**规范化流程**（关键）：
1. 先移除数字内的千分位逗号：`1,234` → `1234`
2. 再移除货币符号：`$`, `%`, `€`, `£`
3. 检查剩余逗号 → 若有则为列表，排序后拼接
4. 否则：去首冠词、去尾标点、合并空格

**3 个难度级别**：Level 1（基础）、Level 2（中等）、Level 3（复杂），共 466 个真实问题。

### 2.3 LLM Judge 评分器（可选）

`LLMJudgeScorer` 是独立的可选评分器，**不在默认 CompositeScorer 管线中**，需要在调用时显式传入。它使用廉价模型对答案质量做三维打分：

| 维度 | 说明 | 分值 |
|------|------|------|
| correctness | 事实正确性 | 1-5 |
| completeness | 覆盖完整性 | 1-5 |
| conciseness | 简洁清晰度 | 1-5 |

综合分数 = sum(scores) / 15，≥0.6 视为通过。提供 `score_async()` 异步方法和 `score()` 同步桩。

## 三、报告输出

### TerminalReporter

ASCII 表格，显示每个任务的范式、总分、通过/失败、各维度得分。

### MarkdownReporter

完整 Markdown 报告，包含：
- 概览（任务数、通过率、平均分）
- 逐任务结果表格（含耗时、步数、评分详情）
- 按类别聚合（各类别通过率和均分）
- 失败列表（含具体未通过维度）

### JSON 导出

```bash
python -m evals --json results.json
```

输出结构化 JSON，包含每个任务的完整评分细节，便于 CI 流水线解析。

## 四、核心数据类型

```python
@dataclass
class EvalTask:
    id: str
    category: str
    description: str
    prompt: str
    expected_tools: list[str]
    expected_in_answer: list[str]
    max_steps: int = 10
    timeout_seconds: int = 120
    paradigms: list[str]          # 允许的范式列表（默认全部）

@dataclass
class EvalResult:
    task_id: str
    category: str
    paradigm: str
    passed: bool = False
    overall_score: float = 0.0    # 0.0 - 1.0
    scores: list[ScoreResult]     # 各维度评分
    tool_events: list[dict]       # 工具执行事件
    tools_used: list[str]
    step_count: int = 0
    content_preview: str = ""     # 回复内容预览（前 200 字符）
    duration_seconds: float = 0.0
    error: str | None = None

@dataclass
class ScoreResult:
    name: str                     # 评分器名称
    value: float                  # 0.0 - 1.0
    passed: bool
    detail: str                   # 可读说明
```

## 五、扩展

### 添加自定义任务

1. 在 `evals/tasks/{category}/` 下创建 `your_task.yaml`
2. 填写 id / description / prompt / expected_tools / expected_in_answer
3. `pytest evals/ -v` 自动发现并生成参数化测试

### 添加新评分器

```python
class MyScorer(Scorer):
    name = "my_scorer"

    def score(self, task: dict, output: dict) -> ScoreResult:
        # 自定义评分逻辑
        return ScoreResult(self.name, score_value, passed_bool, "detail")
```

然后在 `_get_default_scorers()` 中注册即可。

### 添加新基准

参考 `evals/benchmarks/bfcl/` 的模式：
1. `dataset.py` — 实现 `Loader` 类
2. `evaluator.py` — 实现 `Evaluator` 类（`evaluate(agent_factory, ...)` 方法）
3. `metrics.py` — 实现 `Metrics` 类（`compute(results)` 方法）
4. 在 `__main__.py` 的 `_run_benchmark()` 中注册

## 代码调用链

### Layer 1 — 自定义任务评估完整调用链

```
CLI: python -m evals [--paradigm react] [--task file_read_basic] [--output report.md]
  │
  └── main()                                             # __main__.py:284
        ├── argparse 解析 (--paradigm, --task, --output, --json, --benchmark)
        │
        ├── [benchmark 路径] _run_benchmark(args)         # __main__.py:100
        │     └── 见下方「Layer 2 — 社区基准调用链」
        │
        └── [自定义任务路径] _run_custom_tasks(args)      # __main__.py:56
              │
              ├── 1. discover_tasks(tasks_dir)             # runner.py:81
              │     └── _load_task(yaml_path)              # runner.py:96
              │           └── 解析 YAML → EvalTask 实例    # runner.py:43
              │                 (id, category, description, prompt,
              │                  expected_tools, expected_in_answer,
              │                  max_steps, timeout_seconds)
              │
              ├── 2. _create_provider()                   # __main__.py:26
              │     └── OpenAICompatibleProvider(config)   # 从 .env 读取
              │
              ├── 3. _create_tools()                      # __main__.py:39
              │     └── ToolRegistry + discover_tools()   # tools/__init__.py
              │
              ├── 4. _run_agent(task, provider, tools, paradigm)
              │     │                                      # runner.py:118
              │     ├── ToolRegistry.register(tools)
              │     ├── AgentCore(provider, workspace)
              │     │     max_context_tokens=Config.context_window
              │     ├── AgentInput(
              │     │     init_messages=[system_prompt, user_prompt],
              │     │     tools=registry,
              │     │ )
              │     ├── result = await core.run(spec)      # runner.py:264
              │     │     │
              │     │     └── AgentCore 主循环:
              │     │           ├── _call_llm() → provider.chat_with_retry()
              │     │           ├── _execute_tool_calls()  # runner.py:989
              │     │           │     └── ToolRegistry.execute() → ToolGuard.pre_check()
              │     │           └── return AgentOutput(content, tools_used, ...)
              │     │
              │     └── return {content, tools_used, tool_events,
              │                 step_count, error}
              │
              ├── 5. CompositeScorer.score(task, output)   # scorers.py:134
              │     │
              │     ├── CompletionScorer.score()            # scorers.py:41
              │     │     无 error → 1.0, 有 error → 0.0
              │     ├── KeywordScorer.score()               # scorers.py:56
              │     │     hits/expected 比例, ≥0.6 通过
              │     ├── ToolSetScorer.score()               # scorers.py:85
              │     │     Jaccard 相似度 (交集/并集), ≥0.5 通过
              │     └── StepEfficiencyScorer.score()        # scorers.py:112
              │           1 − steps/max_steps
              │     └── 返回 list[ScoreResult(name, value, passed, detail)]
              │
              ├── 6. compute_overall(scores) → EvalResult  # scorers.py:144
              │     加权平均各维度得分 → overall_score (0.0-1.0)
              │
              └── 7. 报告输出
                    ├── TerminalReporter → ASCII 表格
                    ├── MarkdownReporter → .md 文件
                    └── JSON 导出 → .json 文件
```

### Layer 2 — 社区基准调用链

```
CLI: python -m evals --benchmark bfcl --category simple_python --max-samples 20
  │
  └── _run_benchmark(args)                                # __main__.py:100
        │
        ├── [BFCL]                                         # __main__.py:102-148
        │     ├── _check_bfcl_data(data_dir)               # __main__.py:260
        │     │     └── 检查 gorilla/bfcl_eval/data/ 是否存在
        │     │
        │     ├── BFCLLoader(data_dir)                     # benchmarks/bfcl/dataset.py
        │     │     ├── load(category, max_samples)
        │     │     └── load_ground_truth(category)
        │     │
        │     ├── agent_factory = lambda: _BFCLAgent(...)  # __main__.py:201
        │     │     └── _BFCLAgent.run() → AgentCore.run()
        │     │           └── _extract_content() → 提取响应用于 AST 匹配
        │     │
        │     ├── BFCLEvaluator(data_dir)
        │     │     └── evaluate(agent_factory, category, max_samples)
        │     │           ├── _extract_calls(response) → JSON 数组 / 代码块 / 正则回退
        │     │           └── _ast_match(predicted, truth) → 集合比较 AST keys
        │     │
        │     └── BFCLMetrics.compute(results)
        │           └── {overall_accuracy, category_accuracy, error_rate}
        │
        └── [GAIA]                                         # __main__.py:150-194
              ├── GAIALoader(local_data_dir, split)
              │     └── load(level, max_samples)
              │           ├── _load_from_parquet()   # pyarrow 读取 metadata.parquet
              │           └── _load_from_jsonl()     # 回退到 metadata.jsonl
              │
              ├── agent_factory = lambda: _GAIAAgent(...)  # __main__.py:227
              │     └── _GAIAAgent.run() → AgentCore.run()
              │           └── 提示词注入 "FINAL ANSWER: " 格式要求
              │
              ├── GAIAEvaluator(data_dir, split)
              │     └── evaluate(agent_factory, level, max_samples)
              │           ├── _extract_answer(response) → "FINAL ANSWER:" 格式提取
              │           └── _quasi_exact_match(pred, exp) → 规范化后比较
              │                 ├── 移除千分位逗号
              │                 ├── 移除货币符号
              │                 └── 去冠词 / 去标点 / 合并空格
              │
              └── GAIAMetrics.compute(results)
                    └── {exact_match_rate, partial_match_rate, level_accuracy}
```

### pytest CI 集成调用链

```
pytest evals/ -v  [--live-eval]
  │
  └── conftest.py                                          # evals/conftest.py
        ├── pytest_addoption(parser)
        │     └── parser.addoption("--live-eval", ...)     # conftest.py:16
        │
        ├── pytest_generate_tests(metafunc)
        │     ├── discover_tasks() → 参数化 task 列表
        │     └── 为每个 task 生成 test 函数
        │
        └── test_*.py 执行:
              ├── [CI 模式] 仅验证任务加载、数据结构、评分管线
              │     └── 不调用 LLM，仅做 schema 校验
              └── [--live-eval] 完整端到端执行
                    └── _run_custom_tasks() → 完整 Layer 1 流程（同上）
```

### 核心数据类型关系

```
EvalTask (runner.py:43)                    ScoreResult (scorers.py:11)
  ├── id: str                               ├── name: str
  ├── category: str                         ├── value: float (0.0-1.0)
  ├── description: str                      ├── passed: bool
  ├── prompt: str                           └── detail: str
  ├── expected_tools: list[str]
  ├── expected_in_answer: list[str]        CompositeScorer (scorers.py:124)
  ├── max_steps: int                         ├── scorers: list[Scorer]
  └── timeout_seconds: int                   └── score(task, output) → list[ScoreResult]

EvalResult (runner.py:59)                  compute_overall() (scorers.py:144)
  ├── task_id: str                           └── weighted average → float
  ├── category: str
  ├── paradigm: str                         Scorer (ABC) (scorers.py:20)
  ├── passed: bool                            ├── CompletionScorer
  ├── overall_score: float                    ├── KeywordScorer
  ├── scores: list[ScoreResult]               ├── ToolSetScorer
  ├── tool_events: list[dict]                 ├── StepEfficiencyScorer
  ├── tools_used: list[str]                   └── LLMJudgeScorer
  ├── step_count: int
  ├── content_preview: str
  ├── duration_seconds: float
  └── error: str | None
