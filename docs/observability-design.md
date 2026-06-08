# mybot 可观测性模块设计文档

## 一、架构总览

```
┌─────────────────────────────────────────────────────────────┐
│                     可观测性模块 (observability/)             │
├───────────────┬───────────────────┬─────────────────────────┤
│   log.py      │    trace.py       │    metrics.py            │
│   (事件记录)   │   (链路追踪)       │   (指标度量)              │
├───────────────┼───────────────────┼─────────────────────────┤
│ • LogConfig   │ • SpanContext     │ • Counter (只增计数)      │
│ • init_logging│ • Span            │ • Gauge   (瞬时值)       │
│ • 4种事件类型  │ • Tracer          │ • Histogram (分布统计)   │
│ • emit()      │ • contextvars传播  │ • MetricsRegistry        │
│               │ • ctx manager API │ • REGISTRY (11项预设)    │
└───────────────┴───────────────────┴─────────────────────────┘
         │              │                    │
         ▼              ▼                    ▼
    loguru 结构化输出 ← span结束事件 ←── 指标快照
         │              │                    │
         └──────────────┼────────────────────┘
                        ▼
              stderr (彩色) + 文件 (JSON)
```

核心设计原则：

1. **零外部依赖** — Tracer 和 Metrics 全部手写，仅依赖已有的 loguru
2. **三层独立** — 每个层次可单独使用，互不强制耦合
3. **contextvars 传播** — Trace 上下文通过 Python 标准库 `contextvars` 在 async 任务间自动传播，业务代码无需手动传递 trace_id
4. **日志即输出** — span 完成事件和指标快照都通过 loguru 输出，JSON 文件日志可直接被外部采集系统（ELK/Loki）消费

---

## 二、Log 层 — 结构化事件记录

### 2.1 日志配置：`LogConfig` + `init_logging()`

**文件**：`observability/log.py`

`LogConfig` 是一个 dataclass，集中管理所有日志配置：

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `level` | `str` | `"DEBUG"` | 最低输出级别 |
| `json_format` | `bool` | `False` | 文件输出是否 JSON 序列化（始终为 JSON） |
| `log_dir` | `Path \| None` | `None` | 文件日志目录，`None` 则仅控制台输出 |
| `rotation` | `str` | `"10 MB"` | 单文件大小上限，超限自动轮转 |
| `retention` | `str` | `"7 days"` | 旧日志保留时长 |
| `_initialized` | `bool` | `False` | **幂等标记**——防止重复配置 |

`init_logging(config)` 在应用启动时调用一次（`Orchestrator.__init__` 中自动触发）：

```
logger.remove()          # 移除 loguru 默认 handler
logger.add(sys.stderr)   # 控制台：彩色格式，人类可读
logger.add(log_dir/...)  # 文件：JSON 行格式，机器可解析
```

**双输出格式对比**：

控制台（人类可读）：
```
2026-06-07 22:05:29.937 | INFO     | Span                 | Span 'llm.chat' ok (1234.56 ms)
```

文件（JSON，机器可解析）：
```json
{"event_type": "Span", "trace_id": "317a5c87...", "span_name": "llm.chat", "latency_ms": 1234.56, "status": "ok"}
```

**幂等设计**：`LogConfig._initialized` 标记确保多次调用 `init_logging()` 不会重复添加 handler，避免日志重复输出。

### 2.2 结构化事件类型

四种事件 dataclass，对应 Agent 运行时的关键动作：

```python
LLMCallEvent(model, latency_ms, messages_count, tools_count,
             tokens_in, tokens_out, tokens_total, finish_reason, error?)

ToolCallEvent(tool_name, success, latency_ms, error?)

SessionEvent(session_key, action, message_count)
# action ∈ {"created", "resumed", "deleted", "compressed"}

AgentRunEvent(session_key, paradigm, steps, total_latency_ms, stop_reason, error?)
```

### 2.3 事件发射：`emit()`

```python
def emit(event, *, level="INFO"):
    data = _to_dict(event)          # dataclass → dict（递归）
    event_type = type(event).__name__  # 类名作为事件类型标签
    summary = ", ".join(f"{k}={v!r}" for k,v in data.items())
    logger.bind(event_type=event_type, **data).log(level, summary)
```

核心机制：`logger.bind(event_type=..., **fields)` 将所有事件字段注入 `record["extra"]`。在 JSON 文件输出中，`extra` 被序列化为顶层字段，形成结构化日志。

---

## 三、Trace 层 — 异步安全的链路追踪

**文件**：`observability/trace.py`

### 3.1 核心数据结构

```
SpanContext (不可变身份三元组)
├── trace_id: str       # uuid4 hex，标识一次完整的用户请求
├── span_id: str        # uuid4 hex[:16]，标识单个操作
└── parent_span_id: str? # 父 span，null 表示根节点

Span (一次命名操作)
├── name: str           # "llm.chat" / "tool.bash" / "context.build"
├── context: SpanContext
├── start_time: float   # time.monotonic()
├── end_time: float?
├── status: str         # "ok" | "error"
├── attributes: dict    # 任意 {key: value}
├── events: list[dict]  # span 内时间点事件
└── _parent: Span?      # 父节点引用（私有，用于恢复上下文）
```

### 3.2 contextvars 传播机制

**为什么需要 contextvars**：Python asyncio 中，多个协程可能在同一线程交替执行。普通的全局变量会在协程切换时互相覆盖。`contextvars` 是 Python 3.7+ 标准库提供的协程局部存储，每个 `asyncio.Task` 自动拥有独立的 context 副本。

**Tracer 的实现**：

```python
class Tracer:
    def __init__(self):
        self._current_span: ContextVar[Span | None] = \
            ContextVar("_trace_current_span", default=None)
```

当协程 A 调用 `start_span("child")`，它将当前 span 写入 `_current_span`。协程 A 挂起、协程 B 恢复执行时，协程 B 读取 `_current_span` 拿到的仍是协程 B 自己的值——两者互不干扰。

**这意味着业务代码无需显式传参**：

```python
async def _process_once(self, ...):
    with tracer.trace("orchestrator.process"):     # 写 ContextVar
        with tracer.span("context.build"):          # 读 ContextVar → 自动找到父级
            ...
        with tracer.span("dispatcher.resolve"):     # 同上
            ...
        with tracer.span(f"agent.{p}.run"):         # 同上
            output = await agent.run(spec)           # 跨越 await，context 不丢失
```

### 3.3 API 设计：trace vs span

提供两套 API，各有两个层级：

| API | 层级 | 用途 |
|-----|------|------|
| `start_trace(name, **attrs)` | 低层 | 创建新 trace（新的 trace_id），忽略当前上下文 |
| `start_span(name, **attrs)` | 低层 | 创建子 span，继承当前 trace_id |
| `trace(name, **attrs)` | 高层（ctx manager） | `start_trace` → yield → `end_span("ok"/"error")` |
| `span(name, **attrs)` | 高层（ctx manager） | `start_span` → yield → `end_span("ok"/"error")` |

**Context Manager 自动处理异常状态**：

```python
@contextmanager
def span(self, name, **attrs):
    span = self.start_span(name, **attrs)
    try:
        yield span
        self.end_span(span, "ok")    # 正常路径
    except Exception:
        self.end_span(span, "error")  # 异常路径
        raise                         # 重新抛出，不吞异常
```

### 3.4 Span 完成时的输出

`end_span()` 将 span 信息作为结构化日志事件输出：

```python
logger.bind(
    event_type="Span",
    trace_id=span.context.trace_id,
    span_id=span.context.span_id,
    parent_span_id=span.context.parent_span_id,
    span_name=span.name,
    latency_ms=round(span.latency_ms, 3),
    status=span.status,
    **span.attributes,               # 用户注入的业务属性
).info(f"Span {name!r} {status} ({latency_ms:.2f} ms)")
```

**一个完整的 trace 日志看起来像**：

```
DEBUG Trace  a1b2c3... started  name='orchestrator.process'
INFO  Span 'context.build' ok (2.31 ms)      trace_id=a1b2c3 parent_span_id=null
INFO  Span 'dispatcher.resolve' ok (0.87 ms)  trace_id=a1b2c3 parent_span_id=xxx
INFO  Span 'llm.chat' ok (1234.56 ms)          trace_id=a1b2c3 parent_span_id=yyy
INFO  Span 'tool.bash' ok (45.12 ms)           trace_id=a1b2c3 parent_span_id=yyy
INFO  Span 'llm.chat' ok (890.12 ms)           trace_id=a1b2c3 parent_span_id=yyy
INFO  Span 'agent.react.run' ok (2200.00 ms)   trace_id=a1b2c3 parent_span_id=xxx
INFO  Span 'orchestrator.process' ok (2250.00 ms)
```

同一个 `trace_id` 串联所有 span，`parent_span_id` 建立父子关系，最终可通过 `trace_id` + `parent_span_id` 重建调用树。

### 3.5 辅助方法

| 方法 | 用途 |
|------|------|
| `current_span() -> Span \| None` | 获取当前活跃 span |
| `current_trace_id() -> str \| None` | 获取当前 trace_id |
| `set_attribute(key, value)` | 在当前 span 上设置属性（在 context manager 内调用） |
| `add_event(name, **attrs)` | 在当前 span 上添加带时间戳的事件点 |

### 3.6 全局单例

```python
tracer = Tracer()  # 模块级单例，进程内共享
```

所有代码通过 `from observability.trace import tracer` 使用同一个实例，确保 trace 上下文在整个请求链路中不丢失。

---

## 四、Metrics 层 — 聚合指标度量

**文件**：`observability/metrics.py`

### 4.1 三种度量类型

#### Counter（计数器）— 只增不减

```python
class Counter:
    def inc(delta=1)    # 线程安全递增
    def get() -> int    # 读取当前值
```

实现：`threading.Lock` 保护 `_value`，保证多线程安全。

#### Gauge（仪表盘）— 可增可减的瞬时值

```python
class Gauge:
    def set(value)          # 设置绝对值
    def inc(delta=1.0)      # 增加
    def dec(delta=1.0)      # 减少
    def get() -> float      # 读取当前值
```

典型用途：`active_sessions`（会话数）— 创建会话时 `inc()`，删除时 `dec()`。

#### Histogram（直方图）— 分布统计

```python
class Histogram:
    def observe(value)       # 记录一个观测值
    def stats() -> dict      # → {count, sum, min, max, avg, p50, p95, p99}
```

百分位数计算使用**简单索引法**（`int(n * p)`），近似但零开销，适合低到中等吞吐场景。所有观测值存储在内部列表中，`stats()` 调用时排序计算。

### 4.2 MetricsRegistry — 指标注册表

```python
class MetricsRegistry:
    def counter(name, description="", unit="") -> Counter
    def gauge(name, description="", unit="") -> Gauge
    def histogram(name, description="", unit="") -> Histogram
    
    def collect_all() -> MetricsRegistrySnapshot  # 全量快照
    def log_snapshot()                            # 通过 loguru 输出快照
```

**属性式访问**：通过 `__getattr__` 重载，可以像访问属性一样获取指标：

```python
REGISTRY.llm_calls_total.inc()      # 等价于 REGISTRY.get_counter("llm_calls_total").inc()
REGISTRY.llm_latency_ms.observe(x)  # 等价于 REGISTRY.get_histogram("llm_latency_ms").observe(x)
```

实现原理：

```python
def __getattr__(self, name):
    if name in self._counters:  return self._counters[name]
    if name in self._gauges:    return self._gauges[name]
    if name in self._histograms: return self._histograms[name]
    raise AttributeError(f"No metric named {name!r}")
```

### 4.3 预设指标体系

模块加载时自动创建 `REGISTRY` 单例，包含 11 个预定义指标：

| 指标名 | 类型 | 说明 | 更新位置 |
|--------|------|------|----------|
| `llm_calls_total` | Counter | LLM 调用总次数 | `runner._call_llm()` |
| `llm_calls_errors_total` | Counter | LLM 调用失败次数 | `runner._call_llm()` exception 路径 |
| `llm_latency_ms` | Histogram | LLM 每次调用延迟 | `runner._call_llm()` |
| `llm_tokens_total` | Counter | 总 token 消耗 | `runner._call_llm()` |
| `tool_calls_total` | Counter | 工具调用总次数 | `runner._exec_one()` |
| `tool_calls_errors_total` | Counter | 工具调用失败次数 | `runner._exec_one()` 失败路径 |
| `tool_latency_ms` | Histogram | 工具执行延迟 | `runner._exec_one()` |
| `agent_steps` | Histogram | 单次任务步骤数 | `runner.run()` 正常退出 / 耗尽迭代 |
| `agent_errors_total` | Counter | Agent 异常退出次数 | `orchestrator._process_once()` + `runner.run()` |
| `active_sessions` | Gauge | 当前活跃会话数 | `session.py: get_session/delete_session/remove_session` |
| `agent_stall_warnings_total` | Counter | 卡死告警次数 | `runner.run()` 步骤数 ≥ 50 |

---

## 五、集成模式

### 5.1 请求生命周期中的可观测性数据流

```
用户输入
  │
  ▼
Orchestrator.run()                           # emit(SessionEvent: resumed)
  │
  ▼
Orchestrator._process_once()
  │
  ├─ tracer.trace("orchestrator.process")    # 创建 trace root, 生成 trace_id
  │    │
  │    ├─ tracer.span("context.build")       # Span: context.build
  │    │    └─ ContextManager.build_messages()
  │    │
  │    ├─ tracer.span("dispatcher.resolve")  # Span: dispatcher.resolve
  │    │    └─ Dispatcher.resolve()
  │    │
  │    └─ tracer.span("agent.{p}.run")       # Span: agent.react.run
  │         │
  │         └─ AgentCore.run()
  │              │
  │              ├─ step_count++             # 步骤计数 + 卡死检测(≥50)
  │              │
  │              ├─ _call_llm()              # Span: llm.chat
  │              │    │                        REGISTRY.llm_calls_total.inc()
  │              │    │                        REGISTRY.llm_latency_ms.observe()
  │              │    │                        REGISTRY.llm_tokens_total.inc()
  │              │    └─ provider.chat_stream()
  │              │
  │              └─ _execute_tool_calls()     # Span: tool.execute × N
  │                   │                        REGISTRY.tool_calls_total.inc()
  │                   │                        REGISTRY.tool_latency_ms.observe()
  │                   └─ ToolRegistry.execute()
  │
  ├─ REGISTRY.agent_steps.observe(steps)     # 记录本次步骤数
  └─ emit(AgentRunEvent)                      # 结构化事件：Agent 运行总结
```

### 5.2 关键集成代码片段

**Orchestrator 中最外层的 trace 包裹**（`core/orchestrator.py:238-242`）：

```python
with tracer.trace(
    "orchestrator.process",
    session_key=session_key,
    user_input=user_input[:200],      # 截断避免日志过长
):
```

选择 `tracer.trace()` 而非 `tracer.span()` 是因为 Orchestrator 的每次 `_process_once()` 都代表一个新的用户请求，应当创建独立的 trace（新的 `trace_id`），而不是挂到已有的 trace 下面。

**Runner 中 LLM 调用的 span + metrics**（`core/runner.py:220-250`）：

```python
async def _call_llm(self, spec, messages, tool_defs):
    t_start = time.monotonic()
    with tracer.span("llm.chat", model=model, messages_count=len(messages)):
        response = await self.provider.chat_stream(...)
        latency_ms = (time.monotonic() - t_start) * 1000
        REGISTRY.llm_calls_total.inc()
        REGISTRY.llm_latency_ms.observe(latency_ms)
        ...
```

注意：span 通过 context manager 包裹，指标更新在 span 内部进行。异常路径通过 context manager 自动设置 `status = "error"`。

**卡死检测**（`core/runner.py:99-105`）：

```python
if step_count == _STALL_WARNING_STEPS:  # 50
    logger.warning("Agent reached {} steps — possible stall", step_count)
    REGISTRY.agent_stall_warnings_total.inc()
```

当单个 Agent 任务的 LLM 调用 + 工具执行循环步数达到 50 步时，发出告警日志并递增计数器。这为外部监控系统设置告警规则提供了数据基础（例如：Prometheus AlertManager 检测到 `agent_stall_warnings_total` 持续增长时触发通知）。

**会话生命周期 gauge**（`context/session.py`）：

```python
# 新建会话：inc
session = Session(key=key)
REGISTRY.active_sessions.inc()

# 从内存移除：dec
removed = self.sessions.pop(key, None)
if removed is not None:
    REGISTRY.active_sessions.dec()
```

`active_sessions` gauge 精确反映 SessionManager 内存中当前的会话数量。

### 5.3 为什么工具指标只在 Runner 中更新

`tools/registry.py` 的 `ToolRegistry.execute()` 是工具执行的底层入口，但工具指标**不在那里更新**，而是在 Runner 的 `_exec_one()` 中。原因：

1. **Span 上下文** — Runner 调用 `tracer.span("tool.execute")`，span 包裹了整个调用链路（包括 registry 的调度）。如果在 registry 内部再创建 span，会造成 span 嵌套混乱
2. **避免双重计数** — Runner 通过 `result.success` 判断成功/失败，registry 通过 try/except 兜底。两处都计数会导致重复
3. **职责清晰** — Runner 是 "Agent 使用工具的 **行为**" 的观测点，Registry 纯粹是 "找到工具 → 调用工具" 的调度逻辑

---

## 六、导出与消费

### 6.1 当前输出通道

```
可观测性数据
    │
    ├─ stderr ──────── 彩色格式 ──→ 开发者终端实时观察
    │
    └─ JSON 文件 ───── 结构化行 ──→ 日志采集系统 (ELK / Loki / Splunk)
```

### 6.2 后续扩展接口

三个层次各自预留了扩展点：

**Log 层**：添加新的 `*Event` dataclass → 调用 `emit(event)` 即可，无需修改日志配置

**Trace 层**：在 `end_span()` 中增加导出逻辑（例如推送至 Jaeger/Zipkin），span 结构不变

**Metrics 层**：
- `collect_all()` → `MetricsRegistrySnapshot` → 可序列化为 Prometheus exposition 格式
- `log_snapshot()` → 定时调用，周期性输出指标快照到日志文件

---

## 七、测试策略

测试文件位于 `test/observability/`，共 63 个测试用例：

| 文件 | 用例数 | 覆盖内容 |
|------|--------|----------|
| `test_log.py` | 14 | LogConfig 默认值、自定义、幂等性；4种事件 dataclass 字段；`emit()` 不抛异常；`_to_dict()` 递归转换 |
| `test_trace.py` | 24 | SpanContext 创建/父子关系；Span 延迟计算；Tracer start_trace/start_span 隔离性/嵌套；end_span 时间戳/状态/父级恢复；ctx manager 正常/异常路径；contextvars 隔离性；全局 tracer 单例 |
| `test_metrics.py` | 25 | Counter 初始值/递增；Gauge set/inc/dec/负值；Histogram 空/单值/多值/百分位数；MetricsRegistry 工厂方法/属性访问/collect_all/log_snapshot；11项预设指标完整性验证 |

关键测试模式：

- **Tracer 隔离性测试**：创建两个 Tracer 实例，验证各自拥有独立的 context
- **Context manager 异常传播测试**：在 `with tracer.span()` 中抛出异常，验证 span 状态为 "error" 且异常正确 re-raise
- **Metrics 百分位数容差测试**：使用 `in (50.0, 51.0)` 而非 `== 50.0`，承认简单索引法的近似性
