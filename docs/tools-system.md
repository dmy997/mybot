# 工具系统 (Tools System)

## 概述

mybot 的工具系统采用 **基类 + 注册表 + 守卫** 三层架构。每个工具继承 `Tool` 抽象基类，通过 `ToolRegistry` 统一注册和调度，`ToolGuard` 在每次执行前进行安全检查。工具通过 `pkgutil` 自动发现，新增工具只需在 `tools/` 目录添加文件即可被识别。

## 三层架构

```
Tool (ABC)              ← 工具实现者继承此基类
    ↓
ToolRegistry            ← 注册、范围过滤、OpenAI schema 导出、执行调度
    ↓
ToolGuard               ← 执行前安全检查（注入/SSRF/敏感路径）
```

## 一、Tool 抽象基类

`tools/tool.py`

```python
@dataclass
class ToolResult:
    success: bool
    content: str
    error: str | None = None

class Tool(ABC):
    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {}    # JSON Schema

    _scopes: set[str] = {"core", "subagent", "memory"}
    """工具可用的执行上下文"""

    _parallel: bool = True
    """是否可与其他工具并发执行。写操作（bash、file write）设为 False"""

    capabilities: set[Capability] = set()
    """能力声明，空集合 = 纯计算，不受安全检查"""

    @abstractmethod
    async def execute(self, **kwargs) -> ToolResult: ...

    def to_openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
```

### 关键设计

- **_parallel**: 默认 `True`（并发安全）。写入工具（bash、file write）应设为 `False` 以避免竞态条件。
- **_scopes**: 默认 `{"core", "subagent", "memory"}`。通过 `available_in(scope)` 判断可用性。
- **capabilities**: 声明工具能做什么，`ToolGuard` 据此选择安全检查。空集合 = 纯计算，跳过所有检查。

## 二、ToolRegistry

`tools/registry.py`

```python
class ToolRegistry:
    def __init__(self, guard: ToolGuard | None = None):
        self._tools: dict[str, Tool] = {}
        self.guard = guard

    def register(self, tool: Tool): ...
    def unregister(self, name: str): ...
    def get(self, name: str) -> Tool | None: ...
```

### 范围过滤

```python
def for_scope(self, scope: str) -> list[Tool]:
    """返回在指定 scope 内可用的工具"""
    return [t for t in self._tools.values() if t.available_in(scope)]

def get_definitions_for_scope(self, scope: str) -> list[dict]:
    """返回指定 scope 内工具的 OpenAI schema 列表"""
    return [t.to_openai_schema() for t in self.for_scope(scope)]
```

### 执行调度与安全

```python
async def execute(self, name: str, arguments: dict) -> ToolResult:
    tool = self._tools.get(name)
    if tool is None:
        return ToolResult(success=False, content="", error=f"Unknown tool: {name}")

    # ToolGuard 预检查
    if self.guard is not None:
        allowed, reason = self.guard.pre_check(
            tool.name, tool.capabilities, arguments,
        )
        if not allowed:
            return ToolResult(success=False, content="", error=reason)

    # 执行（异常隔离）
    try:
        return await tool.execute(**arguments)
    except Exception as exc:
        return ToolResult(success=False, content="", error=f"Tool '{name}' raised: {exc}")
```

执行流程：查找工具 → ToolGuard 预检查 → 执行 → 异常捕获，任何环节失败都返回 `ToolResult(success=False)`，不会向上抛出异常。

## 三、ToolGuard 安全检查

`tools/guard.py`

### Capability 枚举

```python
class Capability(str, enum.Enum):
    SHELL = "shell"        # 执行 shell 命令 → 注入检测
    NETWORK = "network"    # 出站网络请求 → SSRF 检查
    FILE_WRITE = "write"   # 创建/修改/删除文件 → 敏感路径检查
    FILE_READ = "read"     # 读取文件/目录 → 敏感路径检查
    DELEGATE = "delegate"  # 生成子 Agent → 递归控制
```

### 主入口

```python
class ToolGuard:
    def __init__(self, workspace, scope="core", *, allow_network=True, allow_shell=True):
        ...

    def pre_check(self, tool_name: str, capabilities: set[Capability],
                  arguments: dict) -> tuple[bool, str]:
        """返回 (allowed, reason)。reason 为空字符串时表示通过。"""
```

### 三种安全检查

**SHELL — 命令注入检测** (`_check_command_injection`):

扫描命令字符串，检测 8 种注入模式：

```python
_EXTRA_INJECTION_PATTERNS = [
    r"\$\(.*\)",               # $() 命令替换
    r"`[^`]+`",                # 反引号命令替换
    r"\$\{[^}]+\}",            # ${} 变量扩展
    r"/dev/(tcp|udp)/",        # bash 内建网络伪设备
    r"\becho\b.*\\x[0-9a-fA-F]{2}",  # echo -e 十六进制绕过
    r"\$'\\x[0-9a-fA-F]{2}",   # $'\xHH' 编码绕过
    r"\bsocat\b",              # 反向 shell 瑞士军刀
    r"\bnc\b.*-[lL].*-[eE]",   # netcat 反向 shell
]
```

特殊处理：引号分隔的 heredoc（`<< 'EOF'`）禁用所有 shell 展开，其正文被剥离后再做检测，避免误报。

**NETWORK — SSRF 检测** (`_check_ssrf`):

从所有字符串参数中提取 URL，检查目标主机：

```python
_SSRF_BLOCKED_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "[::1]",
                        "metadata.google.internal", "169.254.169.254"}

_SSRF_BLOCKED_CIDRS = [
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",  # 私有网络
    "169.254.0.0/16", "127.0.0.0/8", "0.0.0.0/8",    # 回环/链路本地
    "224.0.0.0/4", "::1/128", "fe80::/10", "fc00::/7", # 多播/本地
]
```

**FILE_READ / FILE_WRITE — 敏感路径检测** (`_check_sensitive_path`):

阻止访问包含敏感关键字的路径：

```python
_BLOCKED_FILE_EXTENSIONS = {".env", ".pem", ".key", ".p12", ".pfx", ".jks", ".keystore"}

_BLOCKED_PATH_PATTERNS = [
    r"(^|/)\.git/",
    r"(^|/)\.ssh/",
    r"(^|[^a-zA-Z0-9])credentials([^a-zA-Z0-9]|$)",
    r"(^|[^a-zA-Z0-9])secret([^a-zA-Z0-9]|$)",
    r"(^|[^a-zA-Z0-9])password([^a-zA-Z0-9]|$)",
    r"(^|[^a-zA-Z0-9])token([^a-zA-Z0-9]|$)",
    r"(^|/)\.env$",                         # 精确匹配 .env，不匹配 .env.example
    r"(^|/)\.env\.(local|production|staging|development|prod|dev)$",
]
```

### 设计原则

- **默认安全**: 新增工具不声明 capabilities = 纯函数，零安全检查负担
- **同步执行**: 所有检查无 I/O，不增加工具执行延迟
- **范围感知**: 不同 scope（core/subagent/memory）可配置不同的 allow_network/allow_shell 策略

## 四、工具自动发现

`tools/__init__.py`

```python
def discover_tools(workspace=None, *, timeout=60) -> dict[str, Tool]:
    tools: dict[str, Tool] = {}
    tools_dir = Path(__file__).parent
    _skip_modules = {"tool", "registry", "subagent", "memory_tools"}

    for module_info in pkgutil.iter_modules([str(tools_dir)]):
        name = module_info.name
        if name.startswith("_") or name in _skip_modules:
            continue
        module = importlib.import_module(f"tools.{name}")

        for _cls_name, cls in inspect.getmembers(module, inspect.isclass):
            if not (issubclass(cls, Tool) and cls is not Tool):
                continue
            if not cls.name:
                continue  # 抽象中间类

            kwargs = _build_init_kwargs(cls, workspace=workspace, timeout=timeout)
            instance = cls(**kwargs)
            tools[instance.name] = instance

    return tools
```

自动发现规则：
- 扫描 `tools/` 目录下的所有 Python 模块
- 跳过 `_` 开头模块和 `tool`/`registry`/`subagent`/`memory_tools`
- 查找所有 `Tool` 的具体子类（`cls.name` 非空）
- 根据 `__init__` 参数签名动态传参（`workspace`、`timeout`）

## 五、并行 vs 串行执行

`core/runner.py:895-1039`

AgentCore 根据工具的 `_parallel` 属性将工具调用分为两组：

```python
# 分组
parallel_group = [(idx, tc) for idx, tc in enumerate(tool_calls)
                  if tools.get(tc.name) is not None and tools.get(tc.name).parallel]
serial_calls = [(idx, tc) for idx, tc in enumerate(tool_calls)
                if not (tools.get(tc.name) is not None and tools.get(tc.name).parallel)]

# 并行组：asyncio.gather 并发执行
tasks = [_exec_one(tc) for _, tc in parallel_group]
raw_results = await asyncio.gather(*tasks, return_exceptions=True)

# 串行组：逐个执行
for idx, tc in serial_calls:
    result, duration_ms = await _exec_one(tc)
```

结果按原始 tool_call 顺序追加到消息列表，确保与 LLM 期望的 `tool_call_id` 顺序一致。

## 设计要点

- **零知识新增**: 添加不需要能力的工具只需设置 name/description/parameters + 实现 execute
- **能力即策略**: ToolGuard 不维护工具白名单，而是根据声明的能力选择检查策略
- **错误隔离**: 单个工具执行失败不影响 Agent 运行循环，LLM 接收到错误后可自行纠正
- **动态参数注入**: 自动发现时根据 `__init__` 签名决定传哪些参数，工具无需知道调用上下文
