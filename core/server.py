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
import uuid
from pathlib import Path
from typing import Any

from config import Config
from loguru import logger

from observability.metrics import REGISTRY
import observability.persistence as _obs_persistence
from observability.recent import recent

from .message_bus import InboundMessage, MessageBus, OutboundMessage
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
    expected = Config.mybot_api_key
    if not expected:
        return True  # auth disabled
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:] == expected
    return False


def _ws_check_auth(headers: list) -> bool:
    """Return True if the WebSocket connection is authenticated."""
    expected = Config.mybot_api_key
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
    _serve_lock = asyncio.Lock()

    async def _ensure_serve_task(session_key: str) -> None:
        """Start a serve() background task for *session_key* if not already running."""
        task = _serve_tasks.get(session_key)
        if task is not None and not task.done():
            return  # fast path — no lock needed

        async with _serve_lock:
            # Re-check under lock to prevent TOCTOU race
            task = _serve_tasks.get(session_key)
            if task is not None and not task.done():
                return
            _serve_tasks[session_key] = asyncio.create_task(
                orchestrator.serve(bus_msg, session_key)
            )

    def _push_channel(session_key: str) -> str:
        """Dedicated per-session outbound channel for scheduled pushes."""
        return f"push:{session_key}"

    async def _deliver_scheduled(task: Any) -> None:
        """Push a scheduled task's result by injecting its prompt onto the bus.

        Routes the prompt with ``source="push:<session_key>"`` so serve()
        writes every OutboundMessage to a per-session push channel that the
        long-lived ``GET /events/{session_id}`` SSE stream drains.  This
        channel is session-scoped (no correlation-id filtering), so results
        reach any browser tab listening on ``/events`` for that session.
        """
        await _ensure_serve_task(task.session_key)
        await bus_msg.inbound(task.session_key).put(InboundMessage(
            session_key=task.session_key,
            content=task.prompt,
            source=_push_channel(task.session_key),
            correlation_id=uuid.uuid4().hex,
        ))

    orchestrator.scheduled_tasks.set_deliver(_deliver_scheduled)

    # Register HITL on_request callback — pushes hitl_confirm events to
    # the HTTP outbound queue so SSE streams can forward them to the browser.
    if hasattr(orchestrator, "hitl_service"):
        def _on_hitl_request(req: Any) -> None:
            try:
                bus_msg.outbound("http").put_nowait(OutboundMessage(
                    req.session_key, "", "hitl_confirm",
                    {"request_id": req.request_id, "tool_name": req.tool_name,
                     "arguments": req.arguments, "capabilities": list(req.capabilities)},
                ))
                logger.info(
                    "HITL listener pushed to http queue: request_id={!r} tool={!r}",
                    req.request_id, req.tool_name,
                )
            except asyncio.QueueFull:
                logger.warning("HITL confirm event dropped — http queue full")

        orchestrator.hitl_service.add_listener(_on_hitl_request)

    # Register Plan Approval on_request callback — pushes plan_approval
    # events to the HTTP outbound queue for SSE forwarding.
    if hasattr(orchestrator, "plan_approval_service"):
        def _on_plan_approval_request(req: Any) -> None:
            try:
                bus_msg.outbound("http").put_nowait(OutboundMessage(
                    req.session_key, "", "plan_approval",
                    {"request_id": req.request_id, "plan_type": req.plan_type,
                     "plan_content": req.plan_content},
                ))
            except asyncio.QueueFull:
                logger.warning("Plan approval event dropped — http queue full")

        orchestrator.plan_approval_service.add_listener(_on_plan_approval_request)

    # ------------------------------------------------------------------
    # HTTP endpoints
    # ------------------------------------------------------------------

    def _get_logs(limit: int, session_key: str | None) -> list[dict[str, object]]:
        """Merge recent in-memory log events with persisted session data."""
        items = recent.get_logs(min(limit, 500))

        if session_key and _obs_persistence.store is not None:
            recent_matches = sum(
                1 for e in items if e.get("data", {}).get("session_key") == session_key  # type: ignore[union-attr]
            )
            if recent_matches < limit:
                persisted = _obs_persistence.store.load_events(session_key, limit)
                recent_keys = {
                    (e.get("timestamp"), e.get("event_type"))  # type: ignore[union-attr]
                    for e in items
                }
                for evt in persisted:
                    key = (evt.get("timestamp"), evt.get("event_type"))
                    if key not in recent_keys:
                        items.append(evt)
                        recent_keys.add(key)
        elif not session_key and _obs_persistence.store is not None:
            persisted = _obs_persistence.store.load_all_events(limit)
            recent_keys = {
                (e.get("timestamp"), e.get("event_type"))
                for e in items
            }
            for evt in persisted:
                key = (evt.get("timestamp"), evt.get("event_type"))
                if key not in recent_keys:
                    items.append(evt)

        items = sorted(items, key=lambda e: e.get("timestamp", 0), reverse=True)[:limit]  # type: ignore[arg-type,return-value]
        return items

    def _get_traces(limit: int, session_key: str | None) -> list[dict[str, object]]:
        """Merge recent in-memory spans with persisted session data."""
        spans: list[dict[str, object]] = list(recent.get_spans(min(limit, 200)))

        if session_key and _obs_persistence.store is not None:
            recent_span_ids = {s.get("span_id") for s in spans}
            persisted = _obs_persistence.store.load_spans(session_key, limit)
            for s in persisted:
                if s.get("span_id") not in recent_span_ids:
                    spans.append(s)
                    recent_span_ids.add(s.get("span_id"))
        elif not session_key and _obs_persistence.store is not None:
            recent_span_ids = {s.get("span_id") for s in spans}
            persisted = _obs_persistence.store.load_all_spans(limit * 3)
            for s in persisted:
                if s.get("span_id") not in recent_span_ids:
                    spans.append(s)
                    recent_span_ids.add(s.get("span_id"))

        spans = sorted(spans, key=lambda s: s.get("end_time") or 0, reverse=True)[:limit]  # type: ignore[arg-type,return-value]
        return spans

    async def health(request: Request) -> JSONResponse:  # noqa: ARG001
        return JSONResponse({"status": "ok"})

    async def metrics(request: Request) -> JSONResponse:  # noqa: ARG001
        """Return current observability metrics as JSON."""
        snap = REGISTRY.collect_all()
        return JSONResponse({
            "counters": snap.counters,
            "gauges": snap.gauges,
            "histograms": snap.histograms,
        })

    async def logs_endpoint(request: Request) -> JSONResponse:  # noqa: ARG001
        """Return recent structured log events, optionally filtered by session."""
        limit = int(request.query_params.get("limit", "100"))
        session_key = request.query_params.get("session_key")
        return JSONResponse(_get_logs(limit, session_key))

    async def traces_endpoint(request: Request) -> JSONResponse:  # noqa: ARG001
        """Return recent trace spans, optionally filtered by session."""
        limit = int(request.query_params.get("limit", "100"))
        session_key = request.query_params.get("session_key")
        return JSONResponse(_get_traces(limit, session_key))

    async def sessions_obs_endpoint(request: Request) -> JSONResponse:  # noqa: ARG001
        """Return list of sessions that have observability data."""
        if _obs_persistence.store is None:
            return JSONResponse([])
        return JSONResponse(_obs_persistence.store.list_sessions())

    async def chat_sse(request: Request) -> JSONResponse | StreamingResponse:
        if not _check_auth(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        session_id = request.path_params.get("session_id", "default")

        try:
            body = await request.json()
        except Exception:
            body = {}
        message = (body.get("message") or "").strip()
        images = body.get("images") or []

        logger.warning("chat_sse: message={!r}, images_count={}", message[:80] if message else "", len(images))

        if not message and not images:
            return JSONResponse({"error": "message is required"}, status_code=400)

        model = body.get("model")
        temperature = body.get("temperature")
        goal = body.get("goal")
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
                goal=goal,
                images=images,
            ))

            try:
                while True:
                    out = await bus_msg.outbound("http").get()
                    if out is None:
                        break
                    if out.correlation_id != cid and out.msg_type not in ("hitl_confirm", "plan_approval"):
                        continue  # not for this request (except broadcast events using session_key)

                    if out.msg_type == "hitl_confirm":
                        yield _sse_event("hitl_confirm", out.data)
                        continue

                    if out.msg_type == "plan_approval":
                        yield _sse_event("plan_approval", out.data)
                        continue

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
                        snap = REGISTRY.collect_all()
                        metrics = {
                            "counters": snap.counters,
                            "gauges": snap.gauges,
                            "histograms": snap.histograms,
                        }
                        if data.get("error"):
                            # Emit delta first so the Web UI renders the
                            # friendly content, then close with done so it
                            # finalises properly (the Web UI has no handler
                            # for the "error" SSE event).
                            if content:
                                yield _sse_event("delta", {"token": content})
                            yield _sse_event("done", {
                                "content": content,
                                "stop_reason": data.get("stop_reason", "error"),
                                "paradigm": data.get("paradigm", "unknown"),
                                "error": data["error"],
                                "metrics": metrics,
                            })
                        else:
                            yield _sse_event("done", {
                                "content": content,
                                "stop_reason": data.get("stop_reason", "completed"),
                                "paradigm": data.get("paradigm", "unknown"),
                                "usage": data.get("usage", {}),
                                "metrics": metrics,
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

    async def push_events(request: Request) -> StreamingResponse:
        """Long-lived SSE stream for scheduled push results on a session.

        Listens on a dedicated per-session push channel so scheduled delivery
        works without correlation-id filtering.  The client opens this once
        per session and receives all push-generated output (delta / tool
        events / final / error).
        """
        if not _check_auth(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        session_id = request.path_params.get("session_id", "default")
        push_channel = _push_channel(session_id)

        async def event_stream():
            await _ensure_serve_task(session_id)
            queue = bus_msg.outbound(push_channel)

            while True:
                # Exit promptly when the browser tab closes / reloads so we
                # don't leak an idle generator per page load.
                if await request.is_disconnected():
                    break
                try:
                    out = await asyncio.wait_for(queue.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    # Keep-alive so proxies / browsers don't close the connection
                    yield ": keepalive\n\n"
                    continue
                # CancelledError (client disconnect / server shutdown) propagates
                # out of the generator and stops the stream.

                if out is None:
                    break

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
                    snap = REGISTRY.collect_all()
                    metrics = {
                        "counters": snap.counters,
                        "gauges": snap.gauges,
                        "histograms": snap.histograms,
                    }
                    if data.get("error"):
                        if content:
                            yield _sse_event("delta", {"token": content})
                        yield _sse_event("done", {
                            "content": content,
                            "stop_reason": data.get("stop_reason", "error"),
                            "paradigm": data.get("paradigm", "unknown"),
                            "error": data["error"],
                            "metrics": metrics,
                        })
                    else:
                        yield _sse_event("done", {
                            "content": content,
                            "stop_reason": data.get("stop_reason", "completed"),
                            "paradigm": data.get("paradigm", "unknown"),
                            "usage": data.get("usage", {}),
                            "metrics": metrics,
                        })
                    # Keep listening — more pushes may come later
                elif out.msg_type == "error":
                    yield _sse_event("push_error", {"message": str(out.data)})

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

    async def get_session_messages(request: Request) -> JSONResponse:
        if not _check_auth(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        key = request.path_params.get("session_id", "")
        history = orchestrator.ctx.session.get_session_history(key)
        return JSONResponse(history)

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

        async def _run(message: str, model: str | None, temperature: float | None,
                       images: list[str] | None = None) -> None:
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
                    images=images or [],
                ))

                while True:
                    out = await bus_msg.outbound("websocket").get()
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
                        snap = REGISTRY.collect_all()
                        metrics = {
                            "counters": snap.counters,
                            "gauges": snap.gauges,
                            "histograms": snap.histograms,
                        }
                        if data.get("error"):
                            if content:
                                await _send("delta", {"token": content})
                            await _send("done", {
                                "content": content,
                                "stop_reason": data.get("stop_reason", "error"),
                                "paradigm": data.get("paradigm", "unknown"),
                                "error": data["error"],
                                "metrics": metrics,
                            })
                        else:
                            await _send("done", {
                                "content": content,
                                "stop_reason": data.get("stop_reason", "completed"),
                                "paradigm": data.get("paradigm", "unknown"),
                                "usage": data.get("usage", {}),
                                "metrics": metrics,
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
                    images = msg.get("images") or []
                    if not message and not images:
                        await _send("error", {"message": "message is required"})
                        continue
                    model = msg.get("model")
                    temperature = msg.get("temperature")
                    _current_task = asyncio.create_task(_run(message, model, temperature, images))

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

    async def hitl_pending(request: Request) -> JSONResponse:
        """List all pending HITL requests (for cross-process bridge polling)."""
        if not _check_auth(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        hitl_svc = getattr(orchestrator, "hitl_service", None)
        if hitl_svc is None:
            return JSONResponse({"pending": []})
        return JSONResponse({"pending": hitl_svc.get_pending_requests()})

    async def hitl_respond(request: Request) -> JSONResponse:
        """Resolve a pending HITL confirmation request.

        Body: ``{"request_id": "...", "decision": "approved" | "denied"}``
        """
        if not _check_auth(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        try:
            body = await request.json()
        except Exception:
            body = {}

        request_id = (body.get("request_id") or "").strip()
        decision = (body.get("decision") or "denied").strip()

        if not request_id:
            return JSONResponse({"error": "request_id is required"}, status_code=400)

        hitl_svc = getattr(orchestrator, "hitl_service", None)
        if hitl_svc is None:
            return JSONResponse({"error": "HITL service not available"}, status_code=400)

        ok = hitl_svc.respond(request_id, decision)
        if not ok:
            return JSONResponse(
                {"error": "request not found or already resolved"},
                status_code=404,
            )

        return JSONResponse({"status": "ok", "request_id": request_id, "decision": decision})

    async def plan_respond(request: Request) -> JSONResponse:
        """Resolve a pending plan approval request.

        Body: ``{"request_id": "...", "decision": "approved" | "denied" | <edited_text>}``
        """
        if not _check_auth(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        try:
            body = await request.json()
        except Exception:
            body = {}

        request_id = (body.get("request_id") or "").strip()
        decision = (body.get("decision") or "denied").strip()

        if not request_id:
            return JSONResponse({"error": "request_id is required"}, status_code=400)

        plan_svc = getattr(orchestrator, "plan_approval_service", None)
        if plan_svc is None:
            return JSONResponse({"error": "Plan approval service not available"}, status_code=400)

        ok = plan_svc.respond(request_id, decision)
        if not ok:
            return JSONResponse(
                {"error": "request not found or already resolved"},
                status_code=404,
            )

        return JSONResponse({"status": "ok", "request_id": request_id, "decision": decision})

    async def plan_pending(request: Request) -> JSONResponse:
        """List all pending plan approval requests (for cross-process bridge)."""
        if not _check_auth(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        plan_svc = getattr(orchestrator, "plan_approval_service", None)
        if plan_svc is None:
            return JSONResponse({"pending": []})
        return JSONResponse({"pending": plan_svc.get_pending_requests()})

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
        Route("/hitl/respond", hitl_respond, methods=["POST"]),
        Route("/hitl/pending", hitl_pending, methods=["GET"]),
        Route("/plan/respond", plan_respond, methods=["POST"]),
        Route("/plan/pending", plan_pending, methods=["GET"]),
        Route("/metrics", metrics),
        Route("/logs", logs_endpoint),
        Route("/traces", traces_endpoint),
        Route("/observability/sessions", sessions_obs_endpoint),
        Route("/chat/{session_id}", chat_sse, methods=["POST"]),
        Route("/events/{session_id}", push_events, methods=["GET"]),
        Route("/sessions", list_sessions, methods=["GET"]),
        Route("/sessions/{session_id}", get_session, methods=["GET"]),
        Route("/sessions/{session_id}/messages", get_session_messages, methods=["GET"]),
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

    host = Config.mybot_host
    port = Config.mybot_port

    provider = OpenAICompatibleProvider(
        api_key=Config.api_key,
        api_base=Config.api_base,
        name=Config.provider_name,
        default_model=Config.default_model,
    )

    orchestrator = Orchestrator(
        workspace=Config.workspace,
        provider=provider,
        max_context_tokens=Config.context_window,
        max_output_tokens=Config.max_output_tokens,
        warning_buffer_ratio=Config.warning_buffer_ratio,
        auto_compact_buffer_ratio=Config.auto_compact_buffer_ratio,
        block_buffer_ratio=Config.block_buffer_ratio,
        compress_ratio=Config.compress_ratio,
        consolidation_ratio=Config.consolidation_ratio,
        idle_compress_seconds=Config.idle_compress_seconds,
        compress_model=Config.light_model,
    )

    app = create_app(orchestrator)

    try:
        import uvicorn

        async def _serve():
            config = uvicorn.Config(
            app, host=host, port=port, timeout_graceful_shutdown=5,
        )
            server = uvicorn.Server(config)
            await orchestrator.start_services()

            # Startup: clean stale observability files
            if _obs_persistence.store is not None:
                removed = _obs_persistence.store.cleanup_stale_files()
                if removed:
                    logger.info("Cleaned up {} stale observability files on startup", removed)

            async def _periodic_obs_cleanup(interval: int = 3600) -> None:
                while True:
                    await asyncio.sleep(interval)
                    if _obs_persistence.store is not None:
                        removed = _obs_persistence.store.cleanup_stale_files()
                        if removed:
                            logger.debug("Periodic obs cleanup removed {} files", removed)

            cleanup_task = asyncio.create_task(_periodic_obs_cleanup())

            try:
                await server.serve()
            finally:
                cleanup_task.cancel()
                try:
                    await cleanup_task
                except asyncio.CancelledError:
                    pass

        asyncio.run(_serve())
    except ImportError:
        logger.error("uvicorn is required to run the server.  Install with: pip install uvicorn")
        sys.exit(1)


if __name__ == "__main__":
    main()
