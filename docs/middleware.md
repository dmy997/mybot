# 可插拔中间件 (Middleware)

## 概述

mybot 的中间件系统采用**责任链模式**，在 Agent 执行循环的五个关键节点提供拦截点。中间件可以：修改 LLM 请求参数、检查/替换工具调用结果、在 Agent 启动/结束时执行操作、甚至中止执行循环。每个中间件只覆盖自己关心的钩子，其余默认透传。

## 入口

`core/middleware.py`

## MiddlewareContext — 共享上下文

```python
@dataclass
class MiddlewareContext:
    """在每个中间件调用间传递的可变上下文。"""

    messages: list[dict[str, Any]] = field(default_factory=list)
    session_key: str = ""
    step_count: int = 0

    # LLM-call 阶段填充
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    tool_defs: list[dict[str, Any]] | None = None
    llm_response: LLMResponse | None = None

    # Tool-execute 阶段填充
    tool_name: str = ""
    tool_arguments: dict[str, Any] = field(default_factory=dict)
    tool_result: ToolResult | None = None
    tools: ToolRegistry | None = None

    # 任意共享数据（中间件间通信）
    data: dict[str, Any] = field(default_factory=dict)
```

`data` 字段是中间件之间通信的唯一渠道 — 前一个中间件写入，后一个读取。

## AgentMiddleware — 五个钩子点

```python
class AgentMiddleware(ABC):
    """基类，每个钩子有默认 no-op 实现。子类按需覆盖。"""

    async def on_agent_start(self, ctx: MiddlewareContext) -> None:
        """Agent 循环开始前调用一次。"""

    async def on_agent_step(self, ctx: MiddlewareContext, call_next: StepNext) -> bool:
        """每轮迭代调用。返回 False 可中止循环。"""
        return await call_next(ctx)

    async def on_llm_call(self, ctx: MiddlewareContext, call_next: LlmNext) -> LLMResponse:
        """包装 LLM API 调用。可修改 ctx.messages/model/... 之后再 call_next。"""
        return await call_next(ctx)

    async def on_tool_execute(self, ctx: MiddlewareContext, call_next: ToolNext) -> ToolResult:
        """包装工具执行。可检查 ctx.tool_name/arguments。不调用 call_next 即可阻止执行。"""
        return await call_next(ctx)

    async def on_agent_end(self, ctx: MiddlewareContext, output: AgentOutput | None = None) -> None:
        """Agent 循环结束后调用一次（含异常退出）。"""
```

### 钩子语义

| 钩子 | 调用时机 | 可操作 | 可阻止 |
|------|----------|--------|--------|
| `on_agent_start` | 循环前 | 初始化 ctx.data | 否（void） |
| `on_agent_step` | 每轮开始 | 检查步数、条件判断 | 是（返回 False） |
| `on_llm_call` | LLM 调用前后 | 修改 messages/model，检查 response | 是（不调用 call_next） |
| `on_tool_execute` | 工具执行前后 | 检查/修改/缓存工具结果 | 是（返回合成 ToolResult） |
| `on_agent_end` | 循环后 | 清理、持久化、通知 | 否（void） |

## MiddlewareChain — 链式调度

```python
class MiddlewareChain:
    def __init__(self, middlewares: list[AgentMiddleware] | None = None):
        self._middlewares: list[AgentMiddleware] = list(middlewares or [])

    def add(self, mw: AgentMiddleware) -> None:
        self._middlewares.append(mw)
```

### 两种执行模式

**顺序执行**（on_agent_start / on_agent_end）— 不需要包装：

```python
async def run_agent_start(self, ctx):
    for mw in self._middlewares:
        await mw.on_agent_start(ctx)
```

**嵌套链**（on_agent_step / on_llm_call / on_tool_execute）— 责任链嵌套：

```python
async def run_llm_call(self, ctx, handler):
    return await self._build_chain(
        [mw.on_llm_call for mw in self._middlewares], handler, ctx
    )
```

### _build_chain — 递归嵌套核心

```python
@staticmethod
def _build_chain(mw_methods, handler, ctx):
    idx = 0
    async def _dispatch(*, _i: int = 0):
        nonlocal idx
        if _i >= len(mw_methods):
            return await handler(ctx)  # 链路末端：执行实际 handler
        return await mw_methods[_i](ctx, lambda ctx: _dispatch(_i=_i + 1))
    return _dispatch(_i=0)
```

这构建了 `mw[0](ctx, lambda: mw[1](ctx, lambda: ... handler(ctx)))` 的嵌套调用链。每个中间件在其 lambda 前后添加自己的逻辑。

## 在 AgentCore 中的集成

`core/runner.py` 在五个位置调用中间件：

```python
# 1. Agent 启动（跳过断点恢复时）
if _checkpoint is None:
    await mw.run_agent_start(ctx)

# 2. Agent 步骤（可中止循环）
should_continue = await mw.run_agent_step(ctx, _step_handler)
if not should_continue:
    # 中间件要求停止
    output = AgentOutput(...)
    await mw.run_agent_end(ctx, output)
    return output

# 3. LLM 调用（包装请求/响应）
response = await mw.run_llm_call(ctx, _llm_handler)

# 4. 工具执行（包装执行）
result = await mw.run_tool_execute(ctx, _tool_handler)

# 5. Agent 结束
await mw.run_agent_end(ctx, output)
```

## 典型用例

**日志记录**: 覆盖 `on_llm_call` 和 `on_tool_execute`，记录每次 LLM 调用和工具执行的耗时。

**缓存**: 覆盖 `on_tool_execute`，对相同参数跳过执行直接返回缓存结果：

```python
class CacheMiddleware(AgentMiddleware):
    async def on_tool_execute(self, ctx, call_next):
        cache_key = (ctx.tool_name, json.dumps(ctx.tool_arguments))
        if cache_key in self._cache:
            return self._cache[cache_key]
        result = await call_next(ctx)
        self._cache[cache_key] = result
        return result
```

**速率限制**: 覆盖 `on_llm_call`，在 call_next 前插入延迟。

**安全检查**: 覆盖 `on_tool_execute`，检查参数后决定是否调用 call_next。

**会话分析**: 覆盖 `on_agent_end`，将 Agent 运行统计写入数据库。

## 设计要点

- **责任链嵌套**: `_build_chain` 递归构建嵌套闭包，中间件顺序注册，外层包裹内层
- **共享状态**: `MiddlewareContext.data` 是中间件间的唯一通信渠道
- **可中止**: `on_agent_step` 通过返回 False 中止 Agent 循环；`on_llm_call`/`on_tool_execute` 通过不调用 call_next 短路
- **最小侵入**: 继承 `AgentMiddleware`，只覆盖需要的钩子，其余透传
- **断点恢复兼容**: 恢复时不调用 `on_agent_start`，但后续 step/llm/tool 钩子正常触发
