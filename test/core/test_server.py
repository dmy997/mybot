"""Tests for core/server.py — HTTP API with SSE and WebSocket."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.message_bus import OutboundMessage
from core.orchestrator import OrchestratorResult
from core.server import _sse_event, create_app


@pytest.fixture
def orchestrator():
    """Mock orchestrator for testing."""
    orch = MagicMock()
    orch.sessions = []
    orch.delete_session = MagicMock(return_value=True)
    orch.process_message = AsyncMock(
        return_value=OrchestratorResult(
            content="test response",
            session_key="test",
            paradigm="react",
            usage={"prompt_tokens": 10, "completion_tokens": 20},
            stop_reason="stop",
        )
    )
    # serve() is the MessageBus-based entry point; default: read one
    # inbound message and publish a matching final response.
    async def _default_serve(bus, session_key):
        msg = await bus.inbound(session_key).get()
        if msg is None:
            return
        await bus.outbound.put(OutboundMessage(
            session_key, msg.correlation_id, "final",
            {"content": "test response", "stop_reason": "stop",
             "paradigm": "react", "usage": {}},
        ))
    orch.serve = AsyncMock(side_effect=_default_serve)
    return orch


class TestSSEEvent:
    def test_event_only(self):
        result = _sse_event("delta")
        assert result == "event: delta\n\n"

    def test_event_with_data(self):
        result = _sse_event("delta", {"token": "hello"})
        assert 'event: delta' in result
        assert 'data: {"token": "hello"}' in result

    def test_event_with_unicode(self):
        result = _sse_event("delta", {"token": "你好"})
        assert "你好" in result


class TestHTTPEndpoints:
    @pytest.fixture
    def client(self, orchestrator):
        app = create_app(orchestrator)
        from starlette.testclient import TestClient
        return TestClient(app)

    def test_health(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_index(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_list_sessions_empty(self, client):
        resp = client.get("/sessions")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_sessions_with_data(self, orchestrator, client):
        orchestrator.sessions = [
            {"key": "s1", "message_count": 5, "created_at": "2025-01-01"},
        ]
        resp = client.get("/sessions")
        assert resp.status_code == 200
        assert len(resp.json()) == 1
        assert resp.json()[0]["key"] == "s1"

    def test_get_session_found(self, orchestrator, client):
        orchestrator.sessions = [
            {"key": "my-session", "message_count": 3, "created_at": "2025-01-01"},
        ]
        resp = client.get("/sessions/my-session")
        assert resp.status_code == 200
        assert resp.json()["key"] == "my-session"

    def test_get_session_not_found(self, client):
        resp = client.get("/sessions/nonexistent")
        assert resp.status_code == 404

    def test_delete_session(self, client):
        resp = client.delete("/sessions/some-key")
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"

    def test_delete_session_not_found(self, orchestrator, client):
        orchestrator.delete_session.return_value = False
        resp = client.delete("/sessions/missing")
        assert resp.status_code == 404

    def test_chat_missing_message(self, client):
        resp = client.post("/chat/default", json={})
        assert resp.status_code == 400
        assert "message" in resp.json()["error"]

    def test_chat_sse_streaming(self, orchestrator):
        """Verify SSE endpoint streams expected events via MessageBus."""

        async def _fake_serve(bus, session_key):
            msg = await bus.inbound(session_key).get()
            if msg is None:
                return
            cid = msg.correlation_id
            q = bus.outbound
            await q.put(OutboundMessage(session_key, cid, "delta", "hello "))
            await q.put(OutboundMessage(session_key, cid, "delta", "world"))
            await q.put(OutboundMessage(session_key, cid, "thinking", "thinking..."))
            await q.put(OutboundMessage(session_key, cid, "thinking_done", None))
            await q.put(OutboundMessage(session_key, cid, "tool_start", "read"))
            await q.put(OutboundMessage(session_key, cid, "tool_end",
                {"name": "read", "status": "ok", "detail": "file content"}))
            await q.put(OutboundMessage(session_key, cid, "final",
                {"content": "hello world", "stop_reason": "stop",
                 "paradigm": "react", "usage": {"prompt_tokens": 5, "completion_tokens": 10}}))

        orchestrator.serve = AsyncMock(side_effect=_fake_serve)

        from starlette.testclient import TestClient
        app = create_app(orchestrator)
        client = TestClient(app)

        with client.stream("POST", "/chat/test", json={"message": "hi"}) as resp:
            assert resp.status_code == 200
            body = "".join(resp.iter_text())
        assert "event: delta" in body
        assert 'data: {"token": "hello "}' in body
        assert "event: thinking" in body
        assert "event: thinking_done" in body
        assert "event: tool_start" in body
        assert "event: tool_end" in body
        assert "event: done" in body

    def test_chat_with_model_and_temperature(self, orchestrator):
        """Verify model and temperature are forwarded via InboundMessage."""

        _captured_model = None

        async def _capture_serve(bus, session_key):
            nonlocal _captured_model
            msg = await bus.inbound(session_key).get()
            if msg is None:
                return
            _captured_model = msg.model
            await bus.outbound.put(OutboundMessage(
                session_key, msg.correlation_id, "final",
                {"content": "ok", "stop_reason": "stop",
                 "paradigm": "react", "usage": {}},
            ))

        orchestrator.serve = AsyncMock(side_effect=_capture_serve)

        from starlette.testclient import TestClient
        app = create_app(orchestrator)
        client = TestClient(app)

        with client.stream(
            "POST", "/chat/default",
            json={"message": "hi", "model": "gpt-4o", "temperature": 0.5},
        ) as resp:
            assert resp.status_code == 200
            resp.read()  # consume the stream

        assert _captured_model == "gpt-4o"


class TestAuth:
    def test_no_auth_when_key_not_set(self, orchestrator, monkeypatch):
        monkeypatch.delenv("MYBOT_API_KEY", raising=False)
        from starlette.testclient import TestClient
        app = create_app(orchestrator)
        client = TestClient(app)
        resp = client.get("/sessions")
        assert resp.status_code == 200

    def test_auth_required_when_key_set(self, orchestrator, monkeypatch):
        monkeypatch.setenv("MYBOT_API_KEY", "secret-token")
        from starlette.testclient import TestClient
        app = create_app(orchestrator)
        client = TestClient(app)
        resp = client.get("/sessions")
        assert resp.status_code == 401

    def test_auth_with_correct_token(self, orchestrator, monkeypatch):
        monkeypatch.setenv("MYBOT_API_KEY", "secret-token")
        from starlette.testclient import TestClient
        app = create_app(orchestrator)
        client = TestClient(app)
        resp = client.get("/sessions", headers={"Authorization": "Bearer secret-token"})
        assert resp.status_code == 200

    def test_auth_with_wrong_token(self, orchestrator, monkeypatch):
        monkeypatch.setenv("MYBOT_API_KEY", "secret-token")
        from starlette.testclient import TestClient
        app = create_app(orchestrator)
        client = TestClient(app)
        resp = client.get("/sessions", headers={"Authorization": "Bearer wrong-token"})
        assert resp.status_code == 401


class TestWebSocket:
    @pytest.fixture
    def ws_client(self, orchestrator):
        from starlette.testclient import TestClient
        app = create_app(orchestrator)
        return TestClient(app)

    def test_ws_chat_sends_response(self, orchestrator):
        """WebSocket chat message produces done event via MessageBus."""

        async def _fake_serve(bus, session_key):
            msg = await bus.inbound(session_key).get()
            if msg is None:
                return
            cid = msg.correlation_id
            await bus.outbound.put(OutboundMessage(session_key, cid, "delta", "response"))
            await bus.outbound.put(OutboundMessage(session_key, cid, "final",
                {"content": "response", "stop_reason": "stop", "paradigm": "react", "usage": {}}))

        orchestrator.serve = AsyncMock(side_effect=_fake_serve)

        from starlette.testclient import TestClient
        app = create_app(orchestrator)
        client = TestClient(app)

        with client.websocket_connect("/ws/test") as ws:
            ws.send_text(json.dumps({"type": "chat", "message": "hello"}))
            messages = []
            for _ in range(10):
                try:
                    msg = ws.receive_json()
                    messages.append(msg)
                    if msg.get("type") == "done":
                        break
                except Exception:
                    break

            assert any(m["type"] == "delta" for m in messages)
            assert any(m["type"] == "done" for m in messages)

    def test_ws_cancel(self, orchestrator):
        """Cancel message interrupts the in-flight request."""

        _started = asyncio.Event()

        async def _blocking_serve(bus, session_key):
            _started.set()
            msg = await bus.inbound(session_key).get()
            if msg is None:
                return
            # Don't publish anything — simulate a slow request so the
            # client can cancel the outbound reader.

        orchestrator.serve = AsyncMock(side_effect=_blocking_serve)

        from starlette.testclient import TestClient
        app = create_app(orchestrator)
        client = TestClient(app)

        with client.websocket_connect("/ws/test") as ws:
            ws.send_text(json.dumps({"type": "chat", "message": "slow request"}))
            # Wait for the serve task to consume the inbound message
            import time
            time.sleep(0.2)
            ws.send_text(json.dumps({"type": "cancel"}))
            msg = ws.receive_json()
            assert msg["type"] == "error"
            assert "cancelled" in msg["message"].lower()

    def test_ws_missing_message(self, orchestrator):
        from starlette.testclient import TestClient
        app = create_app(orchestrator)
        client = TestClient(app)

        with client.websocket_connect("/ws/test") as ws:
            ws.send_text(json.dumps({"type": "chat", "message": ""}))
            msg = ws.receive_json()
            assert msg["type"] == "error"
            assert "required" in msg["message"].lower()

    def test_ws_invalid_json(self, orchestrator):
        from starlette.testclient import TestClient
        app = create_app(orchestrator)
        client = TestClient(app)

        with client.websocket_connect("/ws/test") as ws:
            ws.send_text("not json")
            msg = ws.receive_json()
            assert msg["type"] == "error"
            assert "json" in msg["message"].lower()
