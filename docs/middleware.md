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

## 代码调用链

### 中间件完整执行流（AgentCore.run() 内）

```
AgentCore.run()                                          # core/runner.py:264
  │
  ├─ MiddlewareChain.__init__(middlewares)               # core/middleware.py:131
  │   └─ self._middlewares = list(middlewares or [])
  │
  ├─ ctx = MiddlewareContext(                            # core/runner.py:280
  │       messages=msgs, session_key=session_key, step_count=0
  │   )
  │
  ├─ [1] await mw.run_agent_start(ctx)                  # core/runner.py:294
  │   └─ for mw in self._middlewares:                   # core/middleware.py:143-145
  │       await mw.on_agent_start(ctx)                   #   顺序执行，无 call_next
  │
  ├─ while step_count < max_iterations:                  # core/runner.py:310
  │   │
  │   ├─ ctx.step_count = step_count
  │   │
  │   ├─ [2] should_continue = await mw.run_agent_step(  # core/runner.py:335
  │   │       ctx, _step_handler
  │   │   )
  │   │   └─ self._build_chain(                          # core/middleware.py:158-160
  │   │       [mw.on_agent_step for mw in middlewares],
  │   │       handler, ctx
  │   │     )
  │   │       └─ _dispatch(_i=0)                         # core/middleware.py:196-202
  │   │           ├─ mw[0].on_agent_step(ctx, lambda: _dispatch(_i=1))
  │   │           │   ├─ 前置逻辑（检查步数/条件）
  │   │           │   ├─ await call_next(ctx) → _dispatch(_i=1)
  │   │           │   │   ├─ mw[1].on_agent_step(ctx, lambda: _dispatch(_i=2))
  │   │           │   │   │   └─ ... → _dispatch(_i=N)
  │   │           │   │   │       └─ await handler(ctx)  ← 实际 handler
  │   │           │   │   │           # handler = _step_handler，即 AgentCore 的步进逻辑
  │   │           │   │   └─ 后置逻辑
  │   │           │   └─ 后置逻辑
  │   │           │
  │   │   if not should_continue:                        # core/runner.py:336
  │   │       await mw.run_agent_end(ctx, output)         # core/runner.py:344
  │   │       return output                               #   中间件中止循环
  │   │
  │   ├─ [3] response = await mw.run_llm_call(           # core/runner.py:368
  │   │       ctx, _llm_handler
  │   │   )
  │   │   └─ self._build_chain(                          # core/middleware.py:167-169
  │   │       [mw.on_llm_call for mw in middlewares],
  │   │       handler, ctx
  │   │     )
  │   │       └─ _dispatch(_i=0)
  │   │           ├─ mw[0].on_llm_call(ctx, lambda: _dispatch(_i=1))
  │   │           │   ├─ 修改 ctx.messages / ctx.model / ctx.temperature 等
  │   │           │   ├─ await call_next(ctx) → ... → handler(ctx)
  │   │           │   │   # handler = _llm_handler → provider.chat() 实际调用
  │   │           │   └─ 检查/修改 ctx.llm_response
  │   │           └─ 返回 LLMResponse
  │   │
  │   │   若 LLM 返回 tool_calls:
  │   │   │
  │   │   ├─ [4] result = await mw.run_tool_execute(     # core/runner.py:1039
  │   │   │       ctx, _tool_handler
  │   │   │   )
  │   │   │   └─ self._build_chain(                      # core/middleware.py:176-178
  │   │   │       [mw.on_tool_execute for mw in middlewares],
  │   │   │       handler, ctx
  │   │   │     )
  │   │   │       └─ _dispatch(_i=0)
  │   │   │           ├─ mw[0].on_tool_execute(ctx, lambda: _dispatch(_i=1))
  │   │   │           │   ├─ 检查 ctx.tool_name / ctx.tool_arguments
  │   │   │           │   ├─ await call_next(ctx) → handler(ctx)
  │   │   │           │   │   # handler = _tool_handler → tool.execute() 实际执行
  │   │   │           │   └─ 检查/缓存/替换 ctx.tool_result
  │   │   │           └─ 返回 ToolResult
  │   │   │
  │   │   └─ 将 tool_result 反馈给 LLM → 回到 [3]
  │   │
  │   └─ step_count += 1
  │
  ├─ [5] await mw.run_agent_end(ctx, output)             # core/runner.py:434/458
  │   └─ for mw in self._middlewares:                   # core/middleware.py:147-151
  │       await mw.on_agent_end(ctx, output)              #   顺序执行，无 call_next
  │
  └─ return output

异常路径:
  except Exception:                                      # core/runner.py:465
      await mw.run_agent_end(ctx, None)                   # core/runner.py:472
      raise
```

### _build_chain 嵌套闭包原理

`core/middleware.py:185-202`

注册 3 个中间件 `[A, B, C]` 时，`_build_chain` 构建等价于：

```python
await A.on_llm_call(ctx, lambda ctx:
    await B.on_llm_call(ctx, lambda ctx:
        await C.on_llm_call(ctx, lambda ctx:
            await handler(ctx)    # ← 实际 LLM 调用在最内层
        )
    )
)
```

每个中间件在 `call_next` 前后添加自己的逻辑。`call_next` 是下一层闭包的入口。中间件可以选择不调用 `call_next` 来短路整个链（如缓存命中时直接返回缓存结果）。

### 五种钩子的调用时机总览

```
AgentCore.run() 时间线
═══════════════════════════════════════════════════════════════

  on_agent_start ─── 一次 ─── 循环开始前（断点恢复时跳过）
  │
  ┌─ while loop ─────────────────────────────────────────┐
  │  on_agent_step ─── 每次迭代开始                       │
  │  on_llm_call    ─── LLM API 调用（可能多次 tool call）│
  │  on_tool_execute ─ 每个 tool call 一次                │
  │  ...                                                  │
  └──────────────────────────────────────────────────────┘
  │
  on_agent_end ─── 一次 ─── 循环结束后（含异常）
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
