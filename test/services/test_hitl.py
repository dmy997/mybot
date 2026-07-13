"""Tests for HITL (human-in-the-loop) service and middleware."""

from __future__ import annotations

import asyncio

import pytest

from services.hitl import (
    HitlMiddleware,
    HitlService,
    _needs_confirmation,
    create_hitl_service_and_middleware,
)
from tools.guard import Capability
from tools.registry import ToolRegistry, ToolResult


# ---------------------------------------------------------------------------
# HitlService tests
# ---------------------------------------------------------------------------


class TestHitlService:
    """Test the HitlService asyncio.Future-based confirmation mechanism."""

    @pytest.mark.asyncio
    async def test_approve_via_listener(self):
        """Listener-based approval: callback triggers respond()."""
        svc = HitlService(timeout_seconds=5)

        def _listener(req):
            svc.respond(req.request_id, "approved")

        svc.add_listener(_listener)

        result = await svc.request_confirmation(
            "sess1", "bash", {"command": "ls"}, {"shell"},
        )
        assert result == "approved"

    @pytest.mark.asyncio
    async def test_deny_via_listener(self):
        """Listener-based denial returns 'denied'."""
        svc = HitlService(timeout_seconds=5)

        def _listener(req):
            svc.respond(req.request_id, "denied")

        svc.add_listener(_listener)

        result = await svc.request_confirmation(
            "sess1", "write_file", {"path": "/tmp/x"}, {"write"},
        )
        assert result == "denied"

    @pytest.mark.asyncio
    async def test_timeout_returns_timeout(self):
        """When no respond() is called, request times out."""
        svc = HitlService(timeout_seconds=0.1)

        result = await svc.request_confirmation(
            "sess1", "bash", {}, {"shell"},
        )
        assert result == "timeout"

    @pytest.mark.asyncio
    async def test_respond_after_timeout_returns_false(self):
        """respond() after timeout should return False."""
        svc = HitlService(timeout_seconds=0.05)
        await svc.request_confirmation("sess1", "bash", {}, {"shell"})
        await asyncio.sleep(0.1)
        ok = svc.respond("anything", "approved")
        assert ok is False

    @pytest.mark.asyncio
    async def test_respond_unknown_request_returns_false(self):
        """respond() with unknown request_id returns False."""
        svc = HitlService(timeout_seconds=5)
        ok = svc.respond("nonexistent", "approved")
        assert ok is False

    @pytest.mark.asyncio
    async def test_respond_twice_returns_false_second_time(self):
        """Double respond() on same request: second call returns False."""
        svc = HitlService(timeout_seconds=5)

        captured_id: str | None = None

        def _listener(req):
            nonlocal captured_id
            captured_id = req.request_id

        svc.add_listener(_listener)

        async def _request():
            return await svc.request_confirmation(
                "sess1", "bash", {}, {"shell"},
            )

        task = asyncio.create_task(_request())
        await asyncio.sleep(0.05)
        ok1 = svc.respond(captured_id, "approved")
        result = await task
        ok2 = svc.respond(captured_id, "denied")

        assert ok1 is True
        assert result == "approved"
        assert ok2 is False

    @pytest.mark.asyncio
    async def test_concurrent_requests_isolation(self):
        """Two concurrent requests are independent."""
        svc = HitlService(timeout_seconds=5)

        ids: dict[str, str] = {}

        def _listener(req):
            ids[req.session_key] = req.request_id

        svc.add_listener(_listener)

        async def _approve(session_key):
            result = await svc.request_confirmation(
                session_key, "bash", {}, {"shell"},
            )
            return result

        t1 = asyncio.create_task(_approve("sess1"))
        t2 = asyncio.create_task(_approve("sess2"))
        await asyncio.sleep(0.05)

        svc.respond(ids["sess1"], "approved")
        svc.respond(ids["sess2"], "denied")

        r1, r2 = await t1, await t2
        assert r1 == "approved"
        assert r2 == "denied"

    @pytest.mark.asyncio
    async def test_pending_requests_property(self):
        """pending_requests reflects current state."""
        svc = HitlService(timeout_seconds=5)

        async def _request():
            return await svc.request_confirmation(
                "sess1", "bash", {}, {"shell"},
            )

        task = asyncio.create_task(_request())
        await asyncio.sleep(0.05)

        pending = svc.pending_requests
        assert len(pending) == 1

        req = list(pending.values())[0]
        svc.respond(req.request_id, "approved")
        await task

        assert len(svc.pending_requests) == 0

    @pytest.mark.asyncio
    async def test_multiple_listeners_all_called(self):
        """All registered listeners are invoked."""
        svc = HitlService(timeout_seconds=5)
        called: list[str] = []

        def _listener1(req):
            called.append("l1")

        def _listener2(req):
            called.append("l2")

        svc.add_listener(_listener1)
        svc.add_listener(_listener2)
        svc.add_listener(lambda req: svc.respond(req.request_id, "approved"))

        await svc.request_confirmation("sess1", "bash", {}, {"shell"})
        assert "l1" in called
        assert "l2" in called


# ---------------------------------------------------------------------------
# HitlMiddleware tests
# ---------------------------------------------------------------------------


class TestHitlMiddleware:
    """Test the HitlMiddleware AgentMiddleware integration."""

    @pytest.fixture
    def tools(self, tmp_path) -> ToolRegistry:
        """A minimal ToolRegistry with bash and read tools."""
        from tools.bash_tool import BashTool
        from tools.file_tools import ReadTool

        registry = ToolRegistry()
        registry.register(BashTool(tmp_path))
        registry.register(ReadTool(tmp_path))
        return registry

    @pytest.mark.asyncio
    async def test_bypass_mode_allows_all(self, tools):
        """In bypass mode, all tools execute immediately."""
        svc = HitlService(timeout_seconds=5)
        mw = HitlMiddleware(svc, mode="bypass")

        called = False

        async def _call_next(ctx):
            nonlocal called
            called = True
            return ToolResult(success=True, content="ok")

        from core.middleware import MiddlewareContext

        ctx = MiddlewareContext(
            session_key="test", tool_name="bash",
            tool_arguments={"command": "ls"}, tools=tools,
        )
        result = await mw.on_tool_execute(ctx, _call_next)
        assert called is True
        assert result.success is True

    @pytest.mark.asyncio
    async def test_confirm_mode_shell_requires_confirmation(self, tools):
        """In confirm mode, shell tools block until approved."""
        svc = HitlService(timeout_seconds=5)
        mw = HitlMiddleware(svc, mode="confirm")

        svc.add_listener(lambda req: svc.respond(req.request_id, "approved"))

        called = False

        async def _call_next(ctx):
            nonlocal called
            called = True
            return ToolResult(success=True, content="ok")

        from core.middleware import MiddlewareContext

        ctx = MiddlewareContext(
            session_key="test", tool_name="bash",
            tool_arguments={"command": "ls"}, tools=tools,
        )
        result = await mw.on_tool_execute(ctx, _call_next)
        assert called is True
        assert result.success is True

    @pytest.mark.asyncio
    async def test_confirm_mode_deny_blocks_execution(self, tools):
        """In confirm mode, denied tools return error without executing."""
        svc = HitlService(timeout_seconds=5)
        mw = HitlMiddleware(svc, mode="confirm")

        svc.add_listener(lambda req: svc.respond(req.request_id, "denied"))

        called = False

        async def _call_next(ctx):
            nonlocal called
            called = True
            return ToolResult(success=True, content="ok")

        from core.middleware import MiddlewareContext

        ctx = MiddlewareContext(
            session_key="test", tool_name="bash",
            tool_arguments={"command": "rm -rf /"}, tools=tools,
        )
        result = await mw.on_tool_execute(ctx, _call_next)
        assert called is False
        assert result.success is False
        assert "denied" in result.error.lower()

    @pytest.mark.asyncio
    async def test_confirm_mode_readonly_passes_through(self, tools):
        """In confirm mode, tools without confirmable caps execute immediately."""
        svc = HitlService(timeout_seconds=5)
        mw = HitlMiddleware(svc, mode="confirm")

        called = False

        async def _call_next(ctx):
            nonlocal called
            called = True
            return ToolResult(success=True, content="read content")

        from core.middleware import MiddlewareContext

        ctx = MiddlewareContext(
            session_key="test", tool_name="read_file",
            tool_arguments={"path": "/tmp/test.txt"}, tools=tools,
        )
        result = await mw.on_tool_execute(ctx, _call_next)
        assert called is True
        assert result.success is True

    @pytest.mark.asyncio
    async def test_bypass_tools_list(self, tools):
        """Tools in bypass_tools skip confirmation even in confirm mode."""
        svc = HitlService(timeout_seconds=5)
        mw = HitlMiddleware(svc, mode="confirm", bypass_tools={"bash"})

        called = False

        async def _call_next(ctx):
            nonlocal called
            called = True
            return ToolResult(success=True, content="ok")

        from core.middleware import MiddlewareContext

        ctx = MiddlewareContext(
            session_key="test", tool_name="bash",
            tool_arguments={"command": "ls"}, tools=tools,
        )
        result = await mw.on_tool_execute(ctx, _call_next)
        assert called is True
        assert result.success is True

    @pytest.mark.asyncio
    async def test_confirm_mode_no_tools_in_registry(self):
        """When tool is not in registry, it passes through (unknown tool = safe)."""
        svc = HitlService(timeout_seconds=5)
        mw = HitlMiddleware(svc, mode="confirm")

        called = False

        async def _call_next(ctx):
            nonlocal called
            called = True
            return ToolResult(success=True, content="ok")

        from core.middleware import MiddlewareContext

        ctx = MiddlewareContext(
            session_key="test", tool_name="unknown_tool",
            tool_arguments={}, tools=None,
        )
        result = await mw.on_tool_execute(ctx, _call_next)
        assert called is True
        assert result.success is True

    @pytest.mark.asyncio
    async def test_timeout_auto_denies(self, tools):
        """Timeout returns an error ToolResult without executing."""
        svc = HitlService(timeout_seconds=0.05)
        mw = HitlMiddleware(svc, mode="confirm")

        called = False

        async def _call_next(ctx):
            nonlocal called
            called = True
            return ToolResult(success=True, content="ok")

        from core.middleware import MiddlewareContext

        ctx = MiddlewareContext(
            session_key="test", tool_name="bash",
            tool_arguments={"command": "ls"}, tools=tools,
        )
        result = await mw.on_tool_execute(ctx, _call_next)
        assert called is False
        assert result.success is False
        assert "timeout" in result.error.lower()


# ---------------------------------------------------------------------------
# _needs_confirmation tests
# ---------------------------------------------------------------------------


class TestNeedsConfirmation:
    """Unit tests for _needs_confirmation capability check."""

    def test_shell_needs_confirmation(self):
        assert _needs_confirmation({Capability.SHELL}) is True

    def test_file_write_needs_confirmation(self):
        assert _needs_confirmation({Capability.FILE_WRITE}) is True

    def test_network_needs_confirmation(self):
        assert _needs_confirmation({Capability.NETWORK}) is True

    def test_delegate_needs_confirmation(self):
        assert _needs_confirmation({Capability.DELEGATE}) is True

    def test_file_read_does_not(self):
        assert _needs_confirmation({Capability.FILE_READ}) is False

    def test_empty_caps_does_not(self):
        assert _needs_confirmation(set()) is False

    def test_mixed_caps_needs_confirmation(self):
        assert _needs_confirmation({Capability.FILE_READ, Capability.SHELL}) is True


# ---------------------------------------------------------------------------
# Factory test
# ---------------------------------------------------------------------------


class TestCreateHitl:
    """Test the factory function."""

    @pytest.mark.asyncio
    async def test_create_with_defaults(self, monkeypatch):
        """create_hitl_service_and_middleware with default env vars."""
        monkeypatch.setenv("HITL_MODE", "bypass")
        monkeypatch.setenv("HITL_BYPASS_TOOLS", "")
        monkeypatch.setenv("HITL_TIMEOUT_SECONDS", "60")

        from config import Config
        Config.reload()

        svc, mw = create_hitl_service_and_middleware()
        assert svc is not None
        assert mw is not None
        assert mw._mode == "bypass"
        assert mw._bypass_tools == set()

    @pytest.mark.asyncio
    async def test_create_confirm_mode(self, monkeypatch):
        """Factory creates correct objects in confirm mode."""
        monkeypatch.setenv("HITL_MODE", "confirm")
        monkeypatch.setenv("HITL_BYPASS_TOOLS", "websearch,grepy")
        monkeypatch.setenv("HITL_TIMEOUT_SECONDS", "90")

        from config import Config
        Config.reload()

        svc, mw = create_hitl_service_and_middleware()
        assert mw._mode == "confirm"
        assert mw._bypass_tools == {"websearch", "grepy"}

    @pytest.mark.asyncio
    async def test_create_unknown_mode_falls_back(self, monkeypatch):
        """Unknown mode falls back to confirm (safe default)."""
        monkeypatch.setenv("HITL_MODE", "invalid")

        from config import Config
        Config.reload()

        svc, mw = create_hitl_service_and_middleware()
        assert mw._mode == "confirm"
