# 可观测系统 (Observability)

## 概述

mybot 提供两种互补的可观测性方案：(1) 内置的零依赖方案（结构化日志 + 内存指标 + span 追踪），开箱即用；(2) 可选的可视化方案（OpenTelemetry → Jaeger），提供完整的 trace 可视化面板。

## 一、结构化日志

`observability/log.py`

基于 loguru，所有日志事件携带类型化字段。

### 配置

```python
@dataclass
class LogConfig:
    level: str = "WARNING"       # 控制台最低级别
    file_level: str = "DEBUG"    # 文件日志级别
    log_dir: Path | None = None  # 日志文件目录
    rotation: str = "10 MB"      # 轮转大小
    retention: str = "7 days"    # 保留时间
    json_format: bool = False    # 文件日志是否为 JSON 格式

def init_logging(config=None):
    # 控制台: 彩色输出，格式 = "时间 | 级别 | 事件类型 | 消息"
    logger.add(sys.stderr, level=config.level, colorize=True, ...)

    # 文件: JSON Lines 格式，始终 DEBUG 级别
    if config.log_dir is not None:
        logger.add(config.log_dir / "mybot_{time}.log", serialize=True, ...)
```

### 结构化事件类型

四种类型化事件，所有字段自动绑定到 loguru 的 `extra`：

```python
@dataclass
class LLMCallEvent:
    model: str
    latency_ms: float
    messages_count: int
    tools_count: int
    tokens_in: int
    tokens_out: int
    tokens_total: int
    finish_reason: str
    error: str | None = None

@dataclass
class ToolCallEvent:
    tool_name: str
    success: bool
    latency_ms: float
    error: str | None = None

@dataclass
class SessionEvent:
    session_key: str
    action: str          # "created" | "resumed" | "deleted" | "compressed"
    message_count: int = 0

@dataclass
class AgentRunEvent:
    session_key: str
    paradigm: str
    steps: int
    total_latency_ms: float
    stop_reason: str
    error: str | None = None
```

### emit 辅助函数

```python
def emit(event, *, level="INFO"):
    """将 dataclass 事件通过 loguru 发出，字段作为 extra 键。"""
    data = _to_dict(event)
    event_type = type(event).__name__
    summary = ", ".join(f"{k}={v!r}" for k, v in data.items())
    logger.bind(event_type=event_type, **data).log(level, summary)
```

## 二、指标系统

`observability/metrics.py`

### 三种指标类型

```python
class Counter:
    """单调递增计数器（线程安全）"""
    def inc(self, delta=1): ...
    def get(self) -> int: ...

class Gauge:
    """可增可减的瞬时值（线程安全）"""
    def set(self, value): ...
    def inc(self, delta=1.0): ...
    def dec(self, delta=1.0): ...

class Histogram:
    """观测值分布，支持百分位统计（线程安全）"""
    def observe(self, value): ...
    def stats(self) -> dict:  # {count, sum, min, max, avg, p50, p95, p99}
```

### MetricsRegistry

```python
class MetricsRegistry:
    def counter(self, name, *, description="", unit=""): ...
    def gauge(self, name, *, description="", unit=""): ...
    def histogram(self, name, *, description="", unit=""): ...

    def collect_all(self) -> MetricsRegistrySnapshot:
        """返回所有指标的当前快照"""
        return MetricsRegistrySnapshot(
            counters={n: c.get() for n, c in self._counters.items()},
            gauges={n: g.get() for n, g in self._gauges.items()},
            histograms={n: h.stats() for n, h in self._histograms.items()},
        )
```

通过 `__getattr__` 支持属性式访问：`REGISTRY.llm_calls_total.inc()`。

### 预定义指标

```python
REGISTRY = MetricsRegistry()

REGISTRY.counter("llm_calls_total")           # LLM 调用总次数
REGISTRY.counter("llm_calls_errors_total")    # LLM 调用失败次数
REGISTRY.histogram("llm_latency_ms")          # LLM 调用延迟
REGISTRY.counter("llm_tokens_total")          # 总 token 消耗
REGISTRY.counter("tool_calls_total")          # 工具调用总次数
REGISTRY.counter("tool_calls_errors_total")   # 工具调用失败次数
REGISTRY.histogram("tool_latency_ms")         # 工具执行延迟
REGISTRY.histogram("agent_steps")             # 每次运行的步数
REGISTRY.counter("agent_errors_total")        # Agent 错误退出次数
REGISTRY.gauge("active_sessions")             # 内存中的会话数
REGISTRY.counter("agent_stall_warnings_total") # 停滞警告次数
```

## 三、追踪系统

`observability/trace.py`

### Span / SpanContext / Tracer

```python
@dataclass
class SpanContext:
    trace_id: str           # 全局唯一 trace ID
    span_id: str            # 当前 span ID (16 位 hex)
    parent_span_id: str | None

@dataclass
class Span:
    name: str
    context: SpanContext
    start_time: float
    end_time: float | None
    status: str = "ok"
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    _parent: Span | None = None

    @property
    def latency_ms(self) -> float: ...
```

### 基于 contextvars 的异步传播

```python
class Tracer:
    def __init__(self):
        self._current_span: contextvars.ContextVar[Span | None] = (
            contextvars.ContextVar("_trace_current_span", default=None)
        )
        self._on_span_start: list[Callable[[Span], None]] = []
        self._on_span_end: list[Callable[[Span], None]] = []

    def start_trace(self, name, **attributes) -> Span:
        """创建新的根 span（新 trace_id）"""
        ...

    def start_span(self, name, **attributes) -> Span:
        """创建子 span，继承当前 trace context；无活跃 span 时自动创建 trace"""
        parent = self._current_span.get()
        ...
```

### 上下文管理器

```python
with tracer.trace("orchestrator.process", session_key="abc"):
    with tracer.span("llm.chat", model="gpt-4"):
        # 执行 LLM 调用
        ...
    # span 结束时自动输出结构化日志事件
```

异常时自动标记 `status="error"` 然后重新抛出：

```python
@contextmanager
def span(self, name, **attributes):
    span = self.start_span(name, **attributes)
    try:
        yield span
        self.end_span(span, "ok")
    except Exception:
        self.end_span(span, "error")
        raise
```

### span 事件与属性

```python
def set_attribute(self, key, value):
    """在活跃 span 上设置属性"""
    s = self._current_span.get()
    if s is not None:
        s.attributes[key] = value

def add_event(self, name, **attributes):
    """在活跃 span 上添加带时间戳的事件"""
    s = self._current_span.get()
    if s is not None:
        s.events.append({"name": name, "timestamp": time.monotonic(), **attributes})
```

### span 完成时的日志输出

```python
def end_span(self, span, status="ok"):
    span.end_time = time.monotonic()
    span.status = status
    self._current_span.set(span._parent)  # 恢复父 span

    # 通知外部桥接（如 OpenTelemetry）
    for hook in self._on_span_end:
        hook(span)

    # 结构化日志输出
    logger.bind(
        event_type="Span",
        trace_id=span.context.trace_id,
        span_id=span.context.span_id,
        parent_span_id=span.context.parent_span_id,
        span_name=span.name,
        latency_ms=round(span.latency_ms, 3),
        status=span.status,
        **span.attributes,
    ).info(f"Span {span.name!r} {span.status} ({span.latency_ms:.2f} ms)")
```

### 全局单例

```python
tracer = Tracer()  # 模块级单例，全进程共享
```

## 四、事件总线订阅者

`observability/subscribers.py`

将 Agent/LLM/Tool 生命周期事件自动桥接到指标和日志：

```python
def install(*, debug=False):
    bus.subscribe(LLMResponseReady, _on_llm_response)
    bus.subscribe(ToolExecutionCompleted, _on_tool_completed)
    bus.subscribe(AgentCompleted, _on_agent_completed)
    bus.subscribe(AgentStallWarning, _on_stall_warning)
```

每个订阅者同时更新 REGISTRY 指标和发出结构化日志：

```python
async def _on_llm_response(event: LLMResponseReady):
    REGISTRY.llm_calls_total.inc()
    REGISTRY.llm_latency_ms.observe(event.latency_ms)
    if event.tokens_total:
        REGISTRY.llm_tokens_total.inc(event.tokens_total)
    if event.finish_reason == "error":
        REGISTRY.llm_calls_errors_total.inc()
    emit(LLMCallEvent(model=event.model, latency_ms=event.latency_ms, ...))
```

## 五、OpenTelemetry 桥接（可选）

`observability/otel_bridge.py`

### 架构

```
自定义 Tracer (contextvars)
    │
    ├── _on_span_start → OTelBridge._on_span_start → 创建 OTel span
    ├── _on_span_end   → OTelBridge._on_span_end   → 同步属性 → 结束 span
    │
    ▼
OTLP HTTP Exporter → Jaeger UI (localhost:16686)
```

### OTelBridge 核心实现

```python
class OTelBridge:
    def install(self, tracer: Tracer) -> bool:
        """注册 _on_span_start/_on_span_end 钩子并初始化 OTel SDK。"""
        if not otel_available():
            return False
        self._setup_otel_sdk()
        tracer._on_span_start.append(self._on_span_start)
        tracer._on_span_end.append(self._on_span_end)
        return True

    def _setup_otel_sdk(self):
        resource = Resource.create({SERVICE_NAME: self._service_name})
        exporter = OTLPSpanExporter(endpoint=self._endpoint)
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        otel_trace.set_tracer_provider(provider)

    def _on_span_start(self, span: Span):
        """创建对应的 OTel span，继承 parent context 维持 trace 树结构。"""
        parent_ctx = set_span_in_context(parent_otel) if parent exists else None
        otel_span = self._otel_tracer.start_span(span.name, attributes=span.attributes, context=parent_ctx)
        self._otel_span_map[span.context.span_id] = otel_span

    def _on_span_end(self, span: Span):
        """同步最终属性 + 事件，结束 OTel span。"""
        otel_span = self._otel_span_map.pop(span.context.span_id, None)
        # 同步 span 结束后才添加的属性（如 tokens_in, tokens_out）
        for key, value in span.attributes.items():
            otel_span.set_attribute(key, value)
        if span.status == "error":
            otel_span.set_status(Status(StatusCode.ERROR))
        otel_span.end()
```

### 自动安装

```python
def auto_install(tracer):
    """当 MYBOT_OTEL_ENABLED=1 时自动安装。"""
    env = os.environ.get("MYBOT_OTEL_ENABLED", "").strip().lower()
    if env not in ("1", "true", "yes"):
        return False
    bridge = OTelBridge()
    return bridge.install(tracer)
```

Orchestrator 在 `__init__` 中调用 `auto_install(tracer)`，无需业务代码改动。

### 使用方式

```bash
docker run -d --name jaeger -p 16686:16686 -p 4318:4318 jaegertracing/all-in-one
pip install "mybot[otel]"
MYBOT_OTEL_ENABLED=1 mybot
# 打开 http://localhost:16686 → Search → Service: mybot → Find Traces
```

每条 trace 展示完整的 `agent.run → llm.chat → tool.execute` 调用树，包括模型名称、token 消耗、工具名称和执行耗时。

## 代码调用链

### 系统启动：可观测性初始化

```
Orchestrator.__init__()                                  # orchestrator.py:92
  │
  ├── init_logging(log_config)                           # orchestrator.py:134 → log.py:54
  │     ├── logger.remove()  # 移除默认 handler
  │     ├── logger.add(sys.stderr, level=config.level, colorize=True)
  │     │     └── 控制台格式: "时间 | 级别 | 事件类型 | 消息"
  │     └── if config.log_dir:
  │           logger.add(log_dir / "mybot_{time}.log", serialize=True)
  │           └── 文件格式: JSON Lines, level=DEBUG
  │
  ├── auto_install_otel(tracer)                          # orchestrator.py:136 → otel_bridge.py:173
  │     │
  │     ├── if MYBOT_OTEL_ENABLED not in ("1", "true", "yes"): return False
  │     └── OTelBridge().install(tracer)                  # otel_bridge.py:84
  │           ├── _setup_otel_sdk()                       # otel_bridge.py:109
  │           │     ├── Resource.create({SERVICE_NAME: ...})
  │           │     ├── OTLPSpanExporter(endpoint=...)
  │           │     └── TracerProvider + BatchSpanProcessor
  │           ├── tracer._on_span_start.append(_on_span_start)
  │           └── tracer._on_span_end.append(_on_span_end)
  │
  └── subscribers.install(debug=...)                     # orchestrator.py (在 start_services 中)
        │                                                # subscribers.py:101
        ├── bus.subscribe(LLMResponseReady, _on_llm_response)    # subscribers.py:29
        ├── bus.subscribe(ToolExecutionCompleted, _on_tool_completed)  # :51
        ├── bus.subscribe(AgentCompleted, _on_agent_completed)        # :66
        └── bus.subscribe(AgentStallWarning, _on_stall_warning)      # :82
```

### Agent 运行时：Span 追踪

```
AgentCore.run()                                          # runner.py:264
  │
  ├── with tracer.trace("agent.run", session_key=..., paradigm=...):
  │     │                                                # trace.py:97 (start_trace)
  │     ├── 创建 SpanContext(trace_id, span_id, parent=None)
  │     ├── _current_span.set(span)  # contextvars 传播
  │     └── [_on_span_start hooks] → OTelBridge 创建 OTel span
  │
  │     while not done:  # Agent 主循环
  │       │
  │       ├── with tracer.span("llm.chat", model=...):   # trace.py:111 (start_span)
  │       │     │
  │       │     ├── parent = _current_span.get()  # 继承 trace context
  │       │     ├── 创建子 Span(name="llm.chat", parent_span_id=parent.span_id)
  │       │     │
  │       │     ├── response = await provider.chat_stream_with_retry(...)
  │       │     │     │
  │       │     │     ├── tracer.set_attribute("tokens_in", usage.prompt_tokens)
  │       │     │     ├── tracer.set_attribute("tokens_out", usage.completion_tokens)
  │       │     │     └── tracer.set_attribute("finish_reason", response.finish_reason)
  │       │     │
  │       │     └── end_span(span, "ok")                  # trace.py:133
  │       │           ├── span.end_time = time.monotonic()
  │       │           ├── _current_span.set(span._parent)  # 恢复父 span
  │       │           ├── [_on_span_end hooks] → OTelBridge 同步属性 + 结束 OTel span
  │       │           └── 结构化日志: logger.bind(event_type="Span", ...).info(...)
  │       │
  │       └── for each tool_call:
  │             with tracer.span("tool.execute", tool_name=...):
  │               │
  │               ├── result = await ToolRegistry.execute(name, arguments)
  │               ├── tracer.set_attribute("tool.success", result.success)
  │               ├── if error: tracer.add_event("tool.error", message=...)
  │               └── end_span(span, "ok" | "error")
  │
  └── end_span(root_span, "ok" | "error")
        └── 最终日志: Span "agent.run" ok (1234.56 ms)
```

### 事件 → 指标 自动桥接

```
AgentCore / ToolRegistry 产生事件
  │
  ├── LLMResponseReady(model, latency_ms, tokens_in, tokens_out, finish_reason, error)
  │     └── bus.emit(event)
  │           └── _on_llm_response(event)                 # subscribers.py:29
  │                 ├── REGISTRY.llm_calls_total.inc()
  │                 ├── REGISTRY.llm_latency_ms.observe(event.latency_ms)
  │                 ├── REGISTRY.llm_tokens_total.inc(event.tokens_total)
  │                 ├── if error: REGISTRY.llm_calls_errors_total.inc()
  │                 └── emit(LLMCallEvent(...))           # log.py:166 → 结构化日志
  │
  ├── ToolExecutionCompleted(tool_name, success, latency_ms, ...)
  │     └── bus.emit(event)
  │           └── _on_tool_completed(event)               # subscribers.py:51
  │                 ├── REGISTRY.tool_calls_total.inc()
  │                 ├── REGISTRY.tool_latency_ms.observe(event.latency_ms)
  │                 ├── if not success: REGISTRY.tool_calls_errors_total.inc()
  │                 └── emit(ToolCallEvent(...))
  │
  ├── AgentCompleted(session_key, paradigm, steps, total_latency_ms, stop_reason, error)
  │     └── bus.emit(event)
  │           └── _on_agent_completed(event)              # subscribers.py:66
  │                 ├── REGISTRY.agent_steps.observe(event.steps)
  │                 ├── if error: REGISTRY.agent_errors_total.inc()
  │                 └── emit(AgentRunEvent(...))
  │
  └── AgentStallWarning(session_key, steps)
        └── bus.emit(event)
              └── _on_stall_warning(event)                # subscribers.py:82
                    ├── REGISTRY.agent_stall_warnings_total.inc()
                    └── logger.warning("Agent stall detected")
```

### 指标采集与暴露

```
REGISTRY (全局单例, metrics.py:205)
  │
  ├── 预定义指标 (metrics.py:207-217):
  │     counters:   llm_calls_total, llm_calls_errors_total,
  │                 llm_tokens_total, tool_calls_total,
  │                 tool_calls_errors_total, agent_errors_total,
  │                 agent_stall_warnings_total
  │     gauges:     active_sessions
  │     histograms: llm_latency_ms, tool_latency_ms, agent_steps
  │
  ├── 属性式访问: REGISTRY.llm_calls_total.inc()          # metrics.py:185
  │     └── __getattr__ → self.counter("llm_calls_total")
  │
  └── HTTP 端点暴露 (server.py):
        ├── GET /metrics → REGISTRY.collect_all()          # metrics.py:164
        │     └── MetricsRegistrySnapshot(counters, gauges, histograms)
        ├── GET /logs    → recent.get_logs()               # recent.py
        └── GET /traces  → recent.get_spans()              # recent.py
```

### Tracer contextvars 异步传播机制

```
Tracer (trace.py:70)
  │
  ├── _current_span: contextvars.ContextVar[Span | None]  # trace.py:73
  │     └── 每个 asyncio.Task 有独立的 context 副本
  │         → serve("s1") 和 serve("s2") 的 span 栈互不干扰
  │
  ├── start_span(name) → 创建子 Span                      # trace.py:111
  │     ├── parent = self._current_span.get()
  │     ├── 若无活跃 span → 自动调用 start_trace() 创建根
  │     └── self._current_span.set(new_span)
  │
  ├── end_span(span, status)                              # trace.py:133
  │     └── self._current_span.set(span._parent)  ← 退栈，恢复父 span
  │
  └── contextmanager span(name):                          # trace.py:186
        ├── start_span() → yield span
        ├── end_span(span, "ok")
        └── except Exception: end_span(span, "error") → raise
```

## 设计要点

- **零依赖内置方案**: 无外部服务时，日志 + 指标 + span 追踪仍然完整工作
- **contextvars 异步传播**: span 按 `asyncio` task 隔离，多并发会话互不干扰
- **属性延迟同步**: span 创建后添加的属性（如 LLM 返回的 token 数）在 `_on_span_end` 中同步到 OTel
- **非侵入式 OTel 集成**: 通过钩子函数实现，自定义 Tracer 对 OTel 零感知
- **事件总线解耦**: 指标更新和日志输出通过 EventBus 订阅者完成，与业务代码完全分离
