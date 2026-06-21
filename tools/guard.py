"""ToolGuard — security middleware for tool execution.

Unified pre-execution check layer.  Each tool declares its *capabilities*
and ToolGuard enforces scope-based policies before the tool runs.

Design:
- Capability defaults to empty set = "pure function", always allowed
- Adding a new tool with no capabilities requires zero security knowledge
- All checks are sync, no I/O, no external dependencies
"""

from __future__ import annotations

import enum
import ipaddress
import re
from pathlib import Path
from typing import Any

from loguru import logger

# ---------------------------------------------------------------------------
# Capability
# ---------------------------------------------------------------------------


class Capability(str, enum.Enum):
    """What a tool can do — used by ToolGuard to decide which checks to apply."""

    SHELL = "shell"        # execute shell commands → injection detection
    NETWORK = "network"    # make outbound network requests → SSRF check
    FILE_WRITE = "write"   # create/modify/delete files → sensitive-path check
    FILE_READ = "read"     # read files or list directories → sensitive-path check
    DELEGATE = "delegate"  # spawn sub-agents → recursion control


# ---------------------------------------------------------------------------
# Blocklists
# ---------------------------------------------------------------------------

_BLOCKED_FILE_EXTENSIONS: frozenset[str] = frozenset({
    ".env", ".pem", ".key", ".p12", ".pfx", ".jks", ".keystore",
})

_BLOCKED_PATH_PATTERNS: list[str] = [
    r"(^|/)\.git(/|$)",
    r"(^|/)\.ssh/",
    # Credential-related keywords as path components (surrounded by
    # non-alphanumeric chars — covers `/`, `_`, `-`, `.`, `$`, `^`)
    r"(^|[^a-zA-Z0-9])credentials([^a-zA-Z0-9]|$)",
    r"(^|[^a-zA-Z0-9])secret([^a-zA-Z0-9]|$)",
    r"(^|[^a-zA-Z0-9])password([^a-zA-Z0-9]|$)",
    r"(^|[^a-zA-Z0-9])token([^a-zA-Z0-9]|$)",
    # Exact .env filename (NOT .env.example / .env.sample)
    r"(^|/)\.env$",
    # Common env-override files with real secrets
    r"(^|/)\.env\.(local|production|staging|development|prod|dev)$",
]

_SSRF_BLOCKED_HOSTS: frozenset[str] = frozenset({
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "[::1]",
    "metadata.google.internal",
    "169.254.169.254",
})

_SSRF_BLOCKED_CIDRS: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("fc00::/7"),
]

def _strip_quoted_heredocs(command: str) -> str:
    """Remove bodies of quoted heredocs from *command*.

    A heredoc with a quoted delimiter (``<< 'EOF'`` or ``<< "EOF"``)
    disables **all** shell expansion — no parameter substitution, no
    command substitution, no backtick evaluation.  The body is literal
    text, so it cannot carry an injection payload.  We replace it with
    a placeholder so downstream injection regexes don't produce false
    positives on harmless content like Markdown backticks.
    """
    result: list[str] = []
    i = 0
    while i < len(command):
        m = re.search(r"<<-?\s*(['\"])(\w+)\1", command[i:])
        if not m:
            result.append(command[i:])
            break
        result.append(command[i:i + m.start()])
        result.append(m.group(0))
        delimiter = m.group(2)
        i = i + m.end()
        remaining = command[i:]
        end_pat = re.compile(
            rf"^[ \t]*{re.escape(delimiter)}\s*$", re.MULTILINE,
        )
        end_m = end_pat.search(remaining)
        if end_m:
            result.append(" [quoted heredoc body omitted] ")
            i = i + end_m.end()
        else:
            result.append(remaining)
            break
    return "".join(result)


# Complements the patterns already in BashTool._DANGEROUS_PATTERNS
_EXTRA_INJECTION_PATTERNS: list[str] = [
    r"\$\(.*\)",               # $() command substitution
    r"`[^`]+`",                # backtick command substitution
    r"\$\{[^}]+\}",            # ${} variable expansion
    r"/dev/(tcp|udp)/",        # bash built-in network pseudo-devices
    r"\becho\b.*\\x[0-9a-fA-F]{2}",  # echo -e '\xHH' encoding bypass
    r"\$'\\x[0-9a-fA-F]{2}",   # $'\xHH' encoding bypass
    r"\bsocat\b",              # swiss-army knife often used for reverse shells
    r"\bnc\b.*-[lL].*-[eE]",   # netcat reverse shell
    r"\bnc\b.*-[lL].*-[cC]",   # netcat connect-back
]

# Protocols allowed in URLs extracted for SSRF checking
_ALLOWED_URL_SCHEMES: frozenset[str] = frozenset({"http", "https"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_urls(text: str) -> list[str]:
    """Extract HTTP/HTTPS URLs from *text* (best-effort regex)."""
    pattern = re.compile(r'https?://[^\s<>"\'`)\]}]+', re.IGNORECASE)
    return pattern.findall(text)


def _host_from_url(url: str) -> str:
    """Extract the host portion from an HTTP(S) URL."""
    # Strip scheme
    rest = url.split("://", 1)[-1]
    # Strip path / query / fragment
    host = rest.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    # Strip port
    if host.startswith("["):
        # IPv6 — port is after the closing bracket
        bracket_end = host.index("]")
        host = host[1:bracket_end]
    elif ":" in host:
        host = host.rsplit(":", 1)[0]
    return host.lower()


def _is_private_ip(host: str) -> bool:
    """Return True if *host* is a private / loopback / link-local address."""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    if addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_unspecified:
        return True
    if addr.is_private:
        return True
    for net in _SSRF_BLOCKED_CIDRS:
        if addr in net:
            return True
    return False


def _match_blocked_path(path_str: str) -> str | None:
    """Return the first matching blocked-path reason, or None."""
    lower = path_str.lower()
    for ext in _BLOCKED_FILE_EXTENSIONS:
        if lower.endswith(ext):
            return f"blocked file extension: {ext}"
    for pat in _BLOCKED_PATH_PATTERNS:
        if re.search(pat, path_str, re.IGNORECASE):
            return f"blocked path pattern: {pat}"
    return None


# ---------------------------------------------------------------------------
# ToolGuard
# ---------------------------------------------------------------------------


class ToolGuard:
    """Pre-execution security check for tool calls.

    Parameters
    ----------
    workspace:
        Root directory for file-path validation.
    scope:
        The execution scope (``"core"``, ``"subagent"``, ``"memory"``).
    allow_network:
        Whether outbound network is allowed in this scope.
    allow_shell:
        Whether shell execution is allowed in this scope.
    """

    def __init__(
        self,
        workspace: str | Path,
        scope: str = "core",
        *,
        allow_network: bool = True,
        allow_shell: bool = True,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.scope = scope
        self.allow_network = allow_network
        self.allow_shell = allow_shell

    # -- main entry -----------------------------------------------------------

    def pre_check(self, tool_name: str, capabilities: set[Capability],
                  arguments: dict[str, Any]) -> tuple[bool, str]:
        """Check whether *tool_name* can execute *arguments*.

        Returns ``(allowed, reason)``.  *reason* is ``""`` when allowed.
        """
        # --- SHELL -----------------------------------------------------------
        if Capability.SHELL in capabilities:
            if not self.allow_shell:
                return False, f"shell exec not allowed in scope '{self.scope}'"
            err = self._check_command_injection(arguments)
            if err:
                logger.warning("ToolGuard: shell injection blocked for '%s': %s",
                               tool_name, err)
                return False, err

        # --- NETWORK ---------------------------------------------------------
        if Capability.NETWORK in capabilities:
            if not self.allow_network:
                return False, f"network access not allowed in scope '{self.scope}'"
            err = self._check_ssrf(arguments)
            if err:
                logger.warning("ToolGuard: SSRF blocked for '%s': %s",
                               tool_name, err)
                return False, err

        # --- FILE_READ / FILE_WRITE -----------------------------------------
        if Capability.FILE_READ in capabilities or Capability.FILE_WRITE in capabilities:
            path = arguments.get("path", "") or arguments.get("file_path", "") or arguments.get("dir_path", "")
            if path:
                err = self._check_sensitive_path(path)
                if err:
                    logger.warning("ToolGuard: sensitive path blocked for '%s': %s",
                                   tool_name, err)
                    return False, err

        return True, ""

    # -- checks (internal) ----------------------------------------------------

    def _check_command_injection(self, arguments: dict[str, Any]) -> str | None:
        """Return an error string if *arguments* show injection patterns."""
        command = arguments.get("command", "")
        if not command or not isinstance(command, str):
            return None

        # Quoted heredocs disable shell expansion — strip their bodies
        # before checking so Markdown backticks etc. don't false-positive.
        command = _strip_quoted_heredocs(command)

        for pattern in _EXTRA_INJECTION_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                return f"injection pattern detected: {pattern}"

        return None

    def _check_ssrf(self, arguments: dict[str, Any]) -> str | None:
        """Return an error string if *arguments* target internal/private hosts."""
        # Scan all string argument values for URLs
        for value in arguments.values():
            if not isinstance(value, str):
                continue
            for url in _extract_urls(value):
                host = _host_from_url(url)
                if not host:
                    continue
                # Block by hostname
                if host in _SSRF_BLOCKED_HOSTS:
                    return f"SSRF blocked: host {host!r}"
                # Block by IP range
                if _is_private_ip(host):
                    return f"SSRF blocked: private IP {host!r}"

        return None

    def _check_sensitive_path(self, path: str) -> str | None:
        """Return an error string if *path* targets a sensitive file/directory."""
        if not path or not isinstance(path, str):
            return None
        return _match_blocked_path(path)
