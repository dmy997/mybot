"""HTTP API server — Starlette app with SSE streaming and WebSocket.

Provides a lightweight HTTP/WS frontend for the Orchestrator.  Start with::

    python -m core.server --port 8080

Requests flow through the MessageBus for decoupled I/O:
  client → InboundMessage → Orchestrator.serve() → OutboundMessage → client

Environment variables
---------------------
``MYBOT_API_KEY``
    Optional Bearer token for authentication.  When not set, all requests
    are allowed without authentication.
``MYBOT_HOST``
    Bind address (default: ``"127.0.0.1"``).
``MYBOT_PORT``
    Listen port (default: ``8080``).
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

from .message_bus import InboundMessage, MessageBus
from .orchestrator import Orchestrator

# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------


def _sse_event(event: str, data: Any = None) -> str:
    """Format an SSE event string."""
    lines = [f"event: {event}"]
    if data is not None:
        payload = json.dumps(data, ensure_ascii=False)
        lines.append(f"data: {payload}")
    lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def _check_auth(request: Any) -> bool:
    """Return True if the request is authenticated."""
    expected = os.getenv("MYBOT_API_KEY", "")
    if not expected:
        return True  # auth disabled
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:] == expected
    return False


def _ws_check_auth(headers: list) -> bool:
    """Return True if the WebSocket connection is authenticated."""
    expected = os.getenv("MYBOT_API_KEY", "")
    if not expected:
        return True
    for key, value in headers:
        if key.decode().lower() == "authorization":
            auth = value.decode()
            if auth.startswith("Bearer "):
                return auth[7:] == expected
    return False


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(orchestrator: Orchestrator, bus_msg: MessageBus | None = None) -> Any:
    """Build the Starlette application.  Requires ``starlette`` to be installed.

    Parameters
    ----------
    orchestrator:
        The top-level Orchestrator instance.
    bus_msg:
        Optional MessageBus for decoupled I/O.  When provided, requests flow
        through the bus instead of calling ``process_message`` directly.
        A default bus is created if omitted.
    """
    try:
        from starlette.applications import Starlette  # noqa: F811
        from starlette.requests import Request
        from starlette.responses import HTMLResponse, JSONResponse, StreamingResponse
        from starlette.routing import Route, WebSocketRoute
        from starlette.websockets import WebSocket, WebSocketDisconnect
    except ImportError:
        raise ImportError(
            "starlette is required for the HTTP API.  Install with: "
            "pip install starlette"
        ) from None

    if bus_msg is None:
        bus_msg = MessageBus()

    # Per-session serve tasks (lazy-started on first request)
    _serve_tasks: dict[str, asyncio.Task[None]] = {}

    async def _ensure_serve_task(session_key: str) -> None:
        """Start a serve() background task for *session_key* if not already running."""
        if session_key not in _serve_tasks or _serve_tasks[session_key].done():
            _serve_tasks[session_key] = asyncio.create_task(
                orchestrator.serve(bus_msg, session_key)
            )

    # ------------------------------------------------------------------
    # HTTP endpoints
    # ------------------------------------------------------------------

    async def health(request: Request) -> JSONResponse:  # noqa: ARG001
        return JSONResponse({"status": "ok"})

    async def chat_sse(request: Request) -> JSONResponse | StreamingResponse:
        if not _check_auth(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        session_id = request.path_params.get("session_id", "default")

        try:
            body = await request.json()
        except Exception:
            body = {}
        message = (body.get("message") or "").strip()
        if not message:
            return JSONResponse({"error": "message is required"}, status_code=400)

        model = body.get("model")
        temperature = body.get("temperature")
        cid = uuid.uuid4().hex

        async def event_stream():
            await _ensure_serve_task(session_id)

            await bus_msg.inbound(session_id).put(InboundMessage(
                session_key=session_id,
                content=message,
                source="http",
                correlation_id=cid,
                model=model,
                temperature=temperature,
            ))

            try:
                while True:
                    out = await bus_msg.outbound.get()
                    if out is None:
                        break
                    if out.correlation_id != cid:
                        continue  # not for this request

                    if out.msg_type == "delta":
                        yield _sse_event("delta", {"token": out.data})
                    elif out.msg_type == "thinking":
                        yield _sse_event("thinking", {"token": out.data})
                    elif out.msg_type == "thinking_done":
                        yield _sse_event("thinking_done", {})
                    elif out.msg_type == "tool_start":
                        yield _sse_event("tool_start", {"name": out.data})
                    elif out.msg_type == "tool_end":
                        yield _sse_event("tool_end", out.data)
                    elif out.msg_type == "tool_exec_start":
                        yield _sse_event("tool_exec_start", out.data)
                    elif out.msg_type == "tool_exec_end":
                        yield _sse_event("tool_exec_end", out.data)
                    elif out.msg_type == "final":
                        data = out.data or {}
                        content = data.get("content", "")
                        if data.get("error"):
                            yield _sse_event("error", {"message": data["error"]})
                        else:
                            yield _sse_event("done", {
                                "content": content,
                                "stop_reason": data.get("stop_reason", "completed"),
                                "paradigm": data.get("paradigm", "unknown"),
                                "usage": data.get("usage", {}),
                            })
                        break  # final — end stream
                    elif out.msg_type == "error":
                        yield _sse_event("error", {"message": out.data})
                        break
            except asyncio.CancelledError:
                raise

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    async def list_sessions(request: Request) -> JSONResponse:
        if not _check_auth(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        sessions = orchestrator.sessions
        return JSONResponse(sessions)

    async def get_session(request: Request) -> JSONResponse:
        if not _check_auth(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        key = request.path_params.get("session_id", "")
        sessions = orchestrator.sessions
        for s in sessions:
            if s.get("key") == key:
                return JSONResponse(s)
        return JSONResponse({"error": "session not found"}, status_code=404)

    async def delete_session(request: Request) -> JSONResponse:
        if not _check_auth(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        key = request.path_params.get("session_id", "")
        ok = orchestrator.delete_session(key)
        if ok:
            return JSONResponse({"status": "deleted", "session_key": key})
        return JSONResponse({"error": "session not found"}, status_code=404)

    # ------------------------------------------------------------------
    # WebSocket
    # ------------------------------------------------------------------

    async def ws_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()

        if not _ws_check_auth(websocket.headers.raw):
            await websocket.send_json({"type": "error", "message": "unauthorized"})
            await websocket.close()
            return

        session_id = websocket.path_params.get("session_id", "default")
        _current_task: asyncio.Task[None] | None = None

        async def _send(event: str, data: Any = None) -> None:
            msg: dict[str, Any] = {"type": event}
            if data is not None:
                msg.update(data)
            await websocket.send_json(msg)

        async def _run(message: str, model: str | None, temperature: float | None) -> None:
            nonlocal _current_task
            cid = uuid.uuid4().hex
            try:
                await _ensure_serve_task(session_id)

                await bus_msg.inbound(session_id).put(InboundMessage(
                    session_key=session_id,
                    content=message,
                    source="websocket",
                    correlation_id=cid,
                    model=model,
                    temperature=temperature,
                ))

                while True:
                    out = await bus_msg.outbound.get()
                    if out is None:
                        break
                    if out.correlation_id != cid:
                        continue

                    if out.msg_type == "delta":
                        await _send("delta", {"token": out.data})
                    elif out.msg_type == "thinking":
                        await _send("thinking", {"token": out.data})
                    elif out.msg_type == "thinking_done":
                        await _send("thinking_done", {})
                    elif out.msg_type == "tool_start":
                        await _send("tool_start", {"name": out.data})
                    elif out.msg_type == "tool_end":
                        await _send("tool_end", out.data)
                    elif out.msg_type == "tool_exec_start":
                        await _send("tool_exec_start", out.data)
                    elif out.msg_type == "tool_exec_end":
                        await _send("tool_exec_end", out.data)
                    elif out.msg_type == "final":
                        data = out.data or {}
                        content = data.get("content", "")
                        if data.get("error"):
                            await _send("error", {"message": data["error"]})
                        else:
                            await _send("done", {
                                "content": content,
                                "stop_reason": data.get("stop_reason", "completed"),
                                "paradigm": data.get("paradigm", "unknown"),
                                "usage": data.get("usage", {}),
                            })
                        break
                    elif out.msg_type == "error":
                        await _send("error", {"message": out.data})
                        break
            except asyncio.CancelledError:
                await _send("error", {"message": "Request cancelled"})

        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await _send("error", {"message": "Invalid JSON"})
                    continue

                msg_type = msg.get("type", "")

                if msg_type == "chat":
                    # Cancel any in-flight request
                    if _current_task is not None and not _current_task.done():
                        _current_task.cancel()
                        try:
                            await _current_task
                        except asyncio.CancelledError:
                            pass

                    message = (msg.get("message") or "").strip()
                    if not message:
                        await _send("error", {"message": "message is required"})
                        continue
                    model = msg.get("model")
                    temperature = msg.get("temperature")
                    _current_task = asyncio.create_task(_run(message, model, temperature))

                elif msg_type == "cancel":
                    if _current_task is not None and not _current_task.done():
                        _current_task.cancel()
                        try:
                            await _current_task
                        except asyncio.CancelledError:
                            pass
                        _current_task = None
                    await _send("error", {"message": "Request cancelled"})

                else:
                    await _send("error", {"message": f"Unknown message type: {msg_type}"})

        except WebSocketDisconnect:
            # Cancel in-flight task on disconnect
            if _current_task is not None and not _current_task.done():
                _current_task.cancel()
                try:
                    await _current_task
                except asyncio.CancelledError:
                    pass

    # ------------------------------------------------------------------
    # UI (cached in closure)
    # ------------------------------------------------------------------

    _ui_html: bytes | None = None

    async def index(request: Request) -> HTMLResponse:
        nonlocal _ui_html
        if _ui_html is None:
            ui_path = Path(__file__).resolve().parent.parent / "server_web" / "index.html"
            _ui_html = ui_path.read_bytes() if ui_path.exists() else b"UI not found"
        return HTMLResponse(_ui_html)

    # ------------------------------------------------------------------
    # Build app
    # ------------------------------------------------------------------

    from starlette.routing import Route, WebSocketRoute

    app = Starlette(routes=[
        Route("/", index),
        Route("/health", health),
        Route("/chat/{session_id}", chat_sse, methods=["POST"]),
        Route("/sessions", list_sessions, methods=["GET"]),
        Route("/sessions/{session_id}", get_session, methods=["GET"]),
        Route("/sessions/{session_id}", delete_session, methods=["DELETE"]),
        WebSocketRoute("/ws/{session_id}", ws_endpoint),
    ])

    return app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Start the HTTP/WS server."""
    import sys

    from config import Config
    from providers.openai_compatible_provider import OpenAICompatibleProvider

    host = os.getenv("MYBOT_HOST", "127.0.0.1")
    port = int(os.getenv("MYBOT_PORT", "8080"))

    provider = OpenAICompatibleProvider(
        api_key=Config.api_key,
        api_base=Config.api_base,
        name=Config.provider_name,
        default_model=Config.default_model,
    )

    orchestrator = Orchestrator(
        workspace=Config.workspace,
        provider=provider,
        compress_model=Config.light_model,
    )

    app = create_app(orchestrator)

    try:
        import uvicorn

        async def _serve():
            config = uvicorn.Config(app, host=host, port=port)
            server = uvicorn.Server(config)
            await orchestrator.start_services()
            await server.serve()

        asyncio.run(_serve())
    except ImportError:
        logger.error("uvicorn is required to run the server.  Install with: pip install uvicorn")
        sys.exit(1)


if __name__ == "__main__":
    main()
