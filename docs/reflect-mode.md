# 反思模式 (Reflect Mode)

## 概述

反思模式是 AgentCore 的一个可选后处理阶段：主回答完成后，附加一次**无工具的反思 LLM 调用**，从事实准确性、逻辑完整性、覆盖度、表述清晰度四个维度自我审查并改进回答质量。

与 Agent 范式（ReAct / Plan-Solve / DeepResearch）正交——任何范式都可叠加反思模式。

## 架构总览

```
用户输入 "/reflect 帮我分析..." 或 Web UI 开启 Reflect 开关
        │
        ▼
Orchestrator.process_message()
  ├── 正则检测 /reflect 前缀 → reflect = True
  ├── 构建 AgentInput(reflect=True, ...)
  │
  ▼
AgentCore.run(spec)
  ├── 主循环: LLM 调用 → 工具执行 → 循环
  ├── 主回答完成 (final_content)
  │
  ├── if spec.reflect and final_content:
  │     _reflect(spec, messages, final_content)
  │       ├── 追加反思 prompt 到消息列表
  │       ├── chat_with_retry(model, tools=[], temperature, max_tokens)
  │       └── 返回修正后的内容 / 失败时返回 None
  │
  └── AgentOutput(
        content=reflected_content or final_content,
        reflected=True/False,
        prereflect_content=final_content,
      )
```

## 完整调用栈

### 入口：Orchestrator.process_message()

`core/orchestrator.py:315-322`

```
_REFLECT_RE = re.compile(r"^/reflect\b", re.IGNORECASE)

if _REFLECT_RE.search(user_input.strip()):
    reflect = True
    user_input = _REFLECT_RE.sub("", user_input.strip()).strip()
    # prefix "/reflect" 被剥离，剩余内容作为正常 user_input
```

`/reflect` 前缀在消息前缀层被检测——它不属于 Dispatcher 四层路由，而是在 `process_message()` 入口处解析。剥离前缀后，剩余消息正常走 Dispatcher → Agent 流程。

### AgentInput 构造

`core/orchestrator.py:436`

```python
spec = AgentInput(
    init_messages=messages,
    tools=filtered_tools,
    model=model,
    reflect=reflect or Config.reflect_enabled,  # 前缀触发 或 全局配置
    ...
)
```

合并逻辑：`reflect` 前缀的优先级高于全局配置。若用户消息以 `/reflect` 开头，`reflect=True`；否则取 `Config.reflect_enabled`（默认 `false`）。

### AgentCore.run() 中的反射触发

`core/runner.py:464-470`

```python
# --- optional reflection pass ---
if spec.reflect and final_content:
    reflected = await self._reflect(spec, messages, final_content)
    if reflected:
        output.prereflect_content = final_content
        output.content = reflected
        output.reflected = True
```

触发条件：
- `spec.reflect` 为 `True`
- 主回答有有效内容（`final_content` 非空）
- 若反射失败（异常或空响应），返回 `None`，保留原始内容

## 核心实现档案

### 1. AgentInput 反射字段

`core/runner.py:70-77`

```python
@dataclass
class AgentInput:
    # ... 其他字段 ...
    reflect: bool = False
    """Enable a post-generation reflection pass that reviews and improves the output."""
    reflect_model: str | None = None
    """Model override for the reflection call (None = same as primary model)."""
    reflect_temperature: float | None = None
    """Temperature override for the reflection call (None = use class default)."""
    reflect_max_tokens: int | None = None
    """Max-tokens override for the reflection call."""
```

### 2. AgentOutput 反射字段

`core/runner.py:91-94`

```python
@dataclass
class AgentOutput:
    # ... 其他字段 ...
    reflected: bool = False
    """Whether the output has been through a reflection pass."""
    prereflect_content: str = ""
    """Content before reflection (for comparison / debugging)."""
```

### 3. 反思 Prompt

`core/runner.py:106-114`

```python
_REFLECTION_PROMPT = (
    "请仔细检查你上面的回答，从以下角度逐一审查：\n"
    "1. **事实准确性** — 是否有事实错误或幻觉？引用的数据、日期、名称是否准确？\n"
    "2. **逻辑完整性** — 推理链条是否有漏洞？结论是否由前面的分析自然推导而来？\n"
    "3. **覆盖度** — 是否遗漏了用户问题中的要点？多角度/多实体是否都覆盖到了？\n"
    "4. **表述清晰度** — 是否简洁明了、无歧义、无冗余？\n"
    "\n"
    "如果发现问题，请给出**修正后的完整回答**（不是补充，是完整替换）。\n"
    "如果没有问题，请简要说明\"已核实无误\"后输出你原有的完整回答。"
)
```

可通过 `REFLECT_PROMPT` 环境变量覆盖。

### 4. 反射默认参数

`core/runner.py:116-117`

```python
_REFLECTION_TEMPERATURE = 0.3
_REFLECTION_MAX_TOKENS = 4096
```

### 5. _reflect() 方法

`core/runner.py:1269-1305`

```python
async def _reflect(
    self,
    spec: AgentInput,
    messages: list[dict[str, Any]],
    content_before: str,
) -> str | None:
    """Run a reflection pass and return improved content, or None on failure."""
    from config import Config

    # 优先级: AgentInput > Config > spec.model
    reflect_model = spec.reflect_model or Config.reflect_model or spec.model
    reflect_temp = (
        spec.reflect_temperature
        if spec.reflect_temperature is not None
        else Config.reflect_temperature
    )
    reflect_max = spec.reflect_max_tokens or Config.reflect_max_tokens

    reflect_prompt = Config.reflect_prompt

    # 追加反思 prompt 到消息列表（user 角色）
    messages.append({"role": "user", "content": reflect_prompt})

    try:
        with tracer.span("agent.reflect", model=reflect_model):
            response = await self.provider.chat_with_retry(
                messages=[dict(m) for m in messages],
                tools=[],           # 关键：无工具
                model=reflect_model,
                max_tokens=reflect_max,
                temperature=reflect_temp,
            )
    except Exception:
        logger.opt(exception=True).warning("Reflection call failed, returning original")
        return None

    if response.content:
        messages.append({"role": "assistant", "content": response.content})
        return response.content
    return None
```

关键设计：

| 设计点 | 说明 |
|--------|------|
| `tools=[]` | 反思阶段不提供任何工具调用能力，确保 LLM 专注于内容审查 |
| `messages=[dict(m) for m in messages]` | 浅拷贝消息列表，避免副作用 |
| 异常返回 `None` | 反思失败不回退为错误——保留原始回答，用户无感知 |
| `messages.append(...)` | 反思 prompt 和回复均追加到消息历史，后续对话可追溯 |
| `tracer.span("agent.reflect")` | 独立的 trace span，可在 Jaeger 中观测 |
| 参数优先级链 | `AgentInput` > `Config` > 默认值 |

### 6. Config 配置项

`config/config.py:234-259`

| 配置项 | 环境变量 | 默认值 | 说明 |
|--------|----------|--------|------|
| `reflect_enabled` | `REFLECT_ENABLED` | `false` | 全局启用反思（可被 `/reflect` 前缀覆盖） |
| `reflect_model` | `REFLECT_MODEL` | `""` | 反思用模型（空 = 使用主模型） |
| `reflect_temperature` | `REFLECT_TEMPERATURE` | `0.3` | 反思温度（低于主回答以实现更严谨的审查） |
| `reflect_max_tokens` | `REFLECT_MAX_TOKENS` | `4096` | 反思最大输出 token |
| `reflect_prompt` | `REFLECT_PROMPT` | 见 \_REFLECTION\_PROMPT | 反思审查 prompt |

### 7. CLI 触发

```
mybot> /reflect 如何看待AI代理的未来发展？
```

`/reflect` 前缀（大小写不敏感）在 `Orchestrator.process_message()` 中被正则 `^/reflect\b` 匹配，剥离后剩余内容作为正常 user_input，同时设置 `reflect=True`。

### 8. Web UI 触发

`server_web/index.html`

```
┌─────────────────────────────────────────────┐
│  [React]  [Plan-Solve]  [DeepResearch]      │
│  [Reflect]  ← 虚线边框的紫色卡片             │
└─────────────────────────────────────────────┘
```

- `reflect-card` 使用虚线边框（`border-style: dashed`），激活时变为实线紫色
- `reflectEnabled` 全局标志切换，`toggleReflect()` 函数控制
- 发送消息时 `reflectEnabled` 为 `true` 则在消息前追加 `/reflect` 前缀

## SSE / WebSocket 流式事件

反思阶段产生以下流式事件（与正常流式相同）：

```
agent.run → [主回答 delta 事件 ...] → agent.reflect → [反思 delta 事件 ...] → done
```

`done` 事件中包含 `reflected: true` 和 `prereflect_content` 字段：

```json
{
  "content": "修正后的回答...",
  "stop_reason": "completed",
  "reflected": true,
  "prereflect_content": "原始回答..."
}
```

Web UI 可在接收到 `done` 事件后显示反思前后的对比（当前未实现对比 UI）。

## 日志与可观测性

```
[trace] agent.run        → 主回答执行
[trace]   agent.reflect  → 反思调用（span 属性: model=reflect_model）
[event]  agent.end       → reflected: true, prereflect_content: "..."
```

反思调用通过 `tracer.span("agent.reflect", model=reflect_model)` 生成独立 span，在 Jaeger 中可看到完整调用树和模型、token 消耗等属性。

## 测试覆盖

`test/core/test_runner.py` — 7 个测试用例覆盖反思模式：

| 测试 | 验证内容 |
|------|---------|
| `test_reflect_false_by_default` | `AgentInput.reflect` 默认值为 `False` |
| `test_output_has_reflection_fields` | `AgentOutput` 包含 `reflected` 和 `prereflect_content` 字段 |
| `test_reflect_enabled_produces_reflected_output` | 启用反思后输出内容被替换为反思结果，`reflected=True` |
| `test_reflect_disabled_skips_reflection` | 不启用反思时 `reflected=False`，`prereflect_content=""` |
| `test_reflect_appends_to_messages` | 反思 prompt 和回复被追加到消息历史 |
| `test_reflect_failure_falls_back_to_original` | 反思调用异常时保留原始内容，`reflected=False` |

## 设计要点

- **与范式正交**：反思模式不是 Agent 范式，而是 AgentCore 的后处理阶段——任何范式（ReAct / Plan-Solve / DeepResearch）的任何回答都可叠加反思
- **无工具反思**：`tools=[]` 确保反思阶段 LLM 不做外部操作，专注内容审查
- **优雅降级**：反思失败（网络异常、速率限制等）时保留原始回答，用户无感知
- **参数优先级链**：`AgentInput` > `Config > 默认值`，提供三层粒度的参数控制
- **两阶段温度差**：主回答使用常规温度，反思阶段使用较低温度（0.3）以提高审查严谨性
- **独立 Model Override**：可为反思阶段指定不同模型（如用能力更强的模型审查主模型的输出），成本可控
- **全局启用 vs 按需启用**：`REFLECT_ENABLED` 为所有请求默认启用反思；`/reflect` 前缀按需启用，互不冲突
- **消息历史可追溯**：反思 prompt 和回复追加到消息列表，后续对话可引用反思结果
- **内容可对比**：`prereflect_content` 保留反思前内容，便于调试和评估反思质量
