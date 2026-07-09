"""Personal WeChat bot channel via iLink HTTP long-poll API.

Provides a ``mybot-wechat`` entry point that authenticates via QR code to the
iLink WeChat bot platform (ilinkai.weixin.qq.com) and bridges messages to the
async Orchestrator via MessageBus.

This channel uses the iLink *bot* API so the bot has its own independent
identity — exactly the "independent mybot friend" model.

Protocol reverse-engineered from ``@tencent-weixin/openclaw-weixin``.

Usage::

    pip install -e ".[server]"
    mybot-wechat
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import signal
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from channels.base import BaseChannel, ChannelMessage, build_orchestrator
from core.message_bus import InboundMessage, MessageBus

# ---------------------------------------------------------------------------
# Protocol constants (from openclaw-weixin types.ts)
# ---------------------------------------------------------------------------

ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5

MESSAGE_TYPE_BOT = 2
MESSAGE_STATE_FINISH = 2

WEIXIN_MAX_MESSAGE_LEN = 4000
CHANNEL_VERSION = "2.1.1"
APP_ID = "bot"

_PERSIST_DEBOUNCE_S = 30.0

ERRCODE_SESSION_EXPIRED = -14
SESSION_PAUSE_DURATION_S = 60 * 60

DEFAULT_LONG_POLL_TIMEOUT_S = 35
MAX_CONSECUTIVE_FAILURES = 3
BACKOFF_DELAY_S = 30
RETRY_DELAY_S = 2
MAX_QR_REFRESH_COUNT = 3

BASE_URL = "https://ilinkai.weixin.qq.com"
_PLATFORM = "wechat"


def _client_version(version: str) -> int:
    """Encode semver as 0x00MMNNPP (major/minor/patch in one uint32)."""
    parts = (version.split(".") + ["0", "0", "0"])[:3]
    major, minor, patch = (int(p) if p.isdigit() else 0 for p in parts)
    return ((major & 0xFF) << 16) | ((minor & 0xFF) << 8) | (patch & 0xFF)


_build_client_version = _client_version  # alias

ILINK_APP_CLIENT_VERSION = _client_version(CHANNEL_VERSION)
BASE_INFO: dict[str, str] = {"channel_version": CHANNEL_VERSION}


def _random_wechat_uin() -> str:
    """X-WECHAT-UIN: random uint32 → decimal string → base64.

    Matches the reference plugin's ``randomWechatUin()`` — fresh per request.
    """
    uint32 = int.from_bytes(os.urandom(4), "big")
    return base64.b64encode(str(uint32).encode()).decode()


def _make_headers(*, token: str = "", auth: bool = True) -> dict[str, str]:
    """Build per-request headers (fresh UIN each call, matching reference)."""
    headers: dict[str, str] = {
        "X-WECHAT-UIN": _random_wechat_uin(),
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "iLink-App-Id": APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }
    if auth and token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _split_text(text: str, max_chars: int) -> list[str]:
    """Split a long message into chunks, preferring newline boundaries."""
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    while len(text) > max_chars:
        split_at = text.rfind("\n", 0, max_chars)
        if split_at == -1:
            split_at = max_chars
        chunks.append(text[:split_at].rstrip())
        text = text[split_at:].lstrip()
    if text:
        chunks.append(text)
    return chunks


# ---------------------------------------------------------------------------
# WechatChannel
# ---------------------------------------------------------------------------


class WechatChannel(BaseChannel):
    """Bridges iLink WeChat HTTP long-poll to the async Orchestrator via MessageBus.

    Authenticates via QR code (bot token stored in ``account.json``), then
    long-polls ``getupdates`` for new messages.  Inbound messages are parsed
    and enqueued on the MessageBus; a consumer task reads outbound replies
    and sends them back through the iLink ``sendmessage`` API.
    """

    channel_name = "wechat"

    def __init__(self, orchestrator) -> None:
        super().__init__(orchestrator)
        self._bus = MessageBus()
        self._client: httpx.AsyncClient | None = None
        self._token: str = ""
        self._stop_event: asyncio.Event | None = None
        self._poll_task: asyncio.Task | None = None
        self._consumer_task: asyncio.Task | None = None
        self._next_poll_timeout_s: int = DEFAULT_LONG_POLL_TIMEOUT_S
        self._session_pause_until: float = 0.0

        # Cursor + dedup
        self._get_updates_buf: str = ""
        self._processed_ids: OrderedDict[str, None] = OrderedDict()

        # Per-user routing state
        self._context_tokens: dict[str, str] = {}
        self._chat_ids: dict[str, str] = {}
        self._serve_tasks: dict[str, asyncio.Task] = {}

        # Persistent state directory (set in start())
        self._state_dir: Path | None = None
        self._last_persist_time: float = 0.0

    # ------------------------------------------------------------------
    # Context token management
    # ------------------------------------------------------------------

    async def _refresh_context_tokens(self) -> bool:
        """Refresh context tokens via getconfig. Returns True on success."""
        if not self._client or not self._token:
            return False
        try:
            data = await self._api_post("ilink/bot/getconfig", {})
            new_tokens: dict[str, str] = data.get("context_tokens", {}) or {}
            if new_tokens:
                cleaned = {
                    str(k): str(v) for k, v in new_tokens.items()
                    if str(k).strip() and str(v).strip()
                }
                self._context_tokens.update(cleaned)
                self._persist_state(force=True)
                logger.info("WechatChannel: refreshed context tokens for {} users", len(new_tokens))
                return True
            return False
        except Exception:
            logger.opt(exception=True).warning("WechatChannel: failed to refresh context tokens")
            return False

    # ------------------------------------------------------------------
    # BaseChannel contract
    # ------------------------------------------------------------------

    async def start(self) -> None:
        logger.info("WechatChannel: starting iLink WeChat bot")

        self._orchestrator.scheduled_tasks.set_deliver(self._deliver_scheduled)
        await self._orchestrator.start_services()

        ws_root = Path(self._orchestrator.workspace)
        self._state_dir = ws_root / "wechat"
        self._state_dir.mkdir(parents=True, exist_ok=True)

        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self._next_poll_timeout_s + 10, connect=30),
            follow_redirects=True,
        )

        # Load existing token or login via QR
        state = self._load_state(self._state_dir)
        self._token = state["token"]
        self._get_updates_buf = state["get_updates_buf"]
        self._context_tokens = state["context_tokens"]

        if not self._token:
            logger.info("WechatChannel: no saved token, starting QR login")
            try:
                ok = await self._qr_login()
            except Exception:
                logger.exception("WechatChannel: QR login failed")
                ok = False
            if not ok:
                logger.error("WechatChannel: login failed, channel not started")
                if self._client:
                    await self._client.aclose()
                    self._client = None
                return
        else:
            logger.info("WechatChannel: loaded saved token")

        self._stop_event = asyncio.Event()
        self._consumer_task = asyncio.create_task(self._consume_outbound())
        self._poll_task = asyncio.create_task(self._poll_loop())

        await self._stop_event.wait()

    async def shutdown(self) -> None:
        logger.info("WechatChannel: shutting down")
        if self._stop_event is not None and not self._stop_event.is_set():
            self._stop_event.set()
        for task in [self._poll_task, self._consumer_task]:
            if task and not task.done():
                task.cancel()
        for task in self._serve_tasks.values():
            task.cancel()
        if self._client:
            await self._client.aclose()
            self._client = None
        await self._bus.close()

    async def send_reply(self, text: str, msg: ChannelMessage) -> None:
        """Send a text reply through the iLink API."""
        if not self._client or not self._token:
            logger.warning("WechatChannel: cannot send reply — not connected")
            return

        ctx_token = self._context_tokens.get(msg.user_id, "")
        if not ctx_token:
            await self._refresh_context_tokens()
            ctx_token = self._context_tokens.get(msg.user_id, "")
        if not ctx_token:
            logger.error(
                "WechatChannel: no context_token for {}, dropping reply", msg.user_id
            )
            return

        chunks = _split_text(text, WEIXIN_MAX_MESSAGE_LEN)
        for chunk in chunks:
            try:
                await self._send_text(msg.chat_id, chunk, ctx_token)
            except Exception:
                logger.opt(exception=True).warning(
                    "WechatChannel: failed to send to {}", msg.chat_id
                )

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    @staticmethod
    def _load_state(state_dir: Path) -> dict[str, Any]:
        state_file = state_dir / "account.json"
        if not state_file.exists():
            return {"token": "", "get_updates_buf": "", "context_tokens": {}, "base_url": BASE_URL}
        try:
            data = json.loads(state_file.read_text())
            ctx = data.get("context_tokens", {})
            if not isinstance(ctx, dict):
                ctx = {}
            return {
                "token": data.get("token", ""),
                "get_updates_buf": data.get("get_updates_buf", ""),
                "context_tokens": {
                    str(k): str(v) for k, v in ctx.items()
                    if str(k).strip() and str(v).strip()
                },
                "base_url": data.get("base_url", BASE_URL),
            }
        except Exception:
            logger.opt(exception=True).warning("WechatChannel: failed to load account.json")
            return {"token": "", "get_updates_buf": "", "context_tokens": {}, "base_url": BASE_URL}

    @staticmethod
    def _save_state(
        state_dir: Path,
        *,
        token: str,
        get_updates_buf: str,
        context_tokens: dict[str, str],
        base_url: str,
    ) -> None:
        state_dir.mkdir(parents=True, exist_ok=True)
        state_file = state_dir / "account.json"
        data = {
            "token": token,
            "get_updates_buf": get_updates_buf,
            "context_tokens": context_tokens,
            "base_url": base_url,
        }
        try:
            state_file.write_text(json.dumps(data, ensure_ascii=False))
        except Exception:
            logger.opt(exception=True).warning("WechatChannel: failed to save account state")

    def _persist_state(self, *, force: bool = False) -> None:
        if self._state_dir is None:
            return
        now = time.time()
        if not force and (now - self._last_persist_time) < _PERSIST_DEBOUNCE_S:
            return
        self._last_persist_time = now
        self._save_state(
            self._state_dir,
            token=self._token,
            get_updates_buf=self._get_updates_buf,
            context_tokens=self._context_tokens,
            base_url=BASE_URL,
        )

    # ------------------------------------------------------------------
    # API helpers
    # ------------------------------------------------------------------

    def _base_url(self) -> str:
        return BASE_URL

    async def _api_get(
        self,
        endpoint: str,
        params: dict | None = None,
        *,
        auth: bool = True,
    ) -> dict:
        assert self._client is not None
        url = f"{self._base_url()}/{endpoint}"
        hdrs = _make_headers(token=self._token, auth=auth)
        resp = await self._client.get(url, params=params, headers=hdrs)
        resp.raise_for_status()
        return resp.json()

    async def _api_get_with_base(
        self,
        base_url: str,
        endpoint: str,
        params: dict | None = None,
        *,
        auth: bool = True,
    ) -> dict:
        assert self._client is not None
        url = f"{base_url.rstrip('/')}/{endpoint}"
        hdrs = _make_headers(token=self._token, auth=auth)
        resp = await self._client.get(url, params=params, headers=hdrs)
        resp.raise_for_status()
        return resp.json()

    async def _api_post(
        self,
        endpoint: str,
        body: dict | None = None,
        *,
        auth: bool = True,
    ) -> dict:
        assert self._client is not None
        url = f"{self._base_url()}/{endpoint}"
        payload = body or {}
        if "base_info" not in payload:
            payload["base_info"] = BASE_INFO
        hdrs = _make_headers(token=self._token, auth=auth)
        resp = await self._client.post(url, json=payload, headers=hdrs)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # QR code login
    # ------------------------------------------------------------------

    async def _fetch_qr_code(self) -> tuple[str, str]:
        """Fetch a fresh QR code.  Returns ``(qrcode_id, scan_url)``."""
        data = await self._api_get(
            "ilink/bot/get_bot_qrcode",
            params={"bot_type": "3"},
            auth=False,
        )
        qrcode_id = data.get("qrcode", "")
        if not qrcode_id:
            raise RuntimeError(f"Failed to get QR code from WeChat API: {data}")
        qrcode_img = data.get("qrcode_img_content", "")
        return qrcode_id, (qrcode_img or qrcode_id)

    @staticmethod
    def _print_qr_code(url: str) -> None:
        try:
            import qrcode as qr_lib

            qr = qr_lib.QRCode(border=1)
            qr.add_data(url)
            qr.make(fit=True)
            qr.print_ascii(invert=True)
        except ImportError:
            print(f"\nLogin URL: {url}\n")

    @staticmethod
    def _is_retryable_poll_error(err: Exception) -> bool:
        if isinstance(err, httpx.TimeoutException | httpx.TransportError):
            return True
        if isinstance(err, httpx.HTTPStatusError):
            code = err.response.status_code if err.response is not None else 0
            return code >= 500
        return False

    async def _qr_login(self) -> bool:
        """QR code login flow.  Returns True on success (token saved to state)."""
        try:
            refresh_count = 0
            qrcode_id, scan_url = await self._fetch_qr_code()
            self._print_qr_code(scan_url)
            current_base_url = self._base_url()

            while True:
                try:
                    status_data = await self._api_get_with_base(
                        base_url=current_base_url,
                        endpoint="ilink/bot/get_qrcode_status",
                        params={"qrcode": qrcode_id},
                        auth=False,
                    )
                except Exception as e:
                    if self._is_retryable_poll_error(e):
                        await asyncio.sleep(1)
                        continue
                    raise

                if not isinstance(status_data, dict):
                    await asyncio.sleep(1)
                    continue

                status = status_data.get("status", "")
                if status == "confirmed":
                    token = status_data.get("bot_token", "")
                    bot_id = status_data.get("ilink_bot_id", "")
                    _base_url = status_data.get("baseurl", "")
                    user_id = status_data.get("ilink_user_id", "")
                    if token:
                        self._token = token
                        self._persist_state(force=True)
                        logger.info(
                            "WechatChannel: login success bot_id={} user_id={}",
                            bot_id,
                            user_id,
                        )
                        return True
                    else:
                        logger.error("WechatChannel: confirmed but no bot_token")
                        return False

                elif status == "scaned_but_redirect":
                    redirect_host = str(status_data.get("redirect_host", "") or "").strip()
                    if redirect_host:
                        if redirect_host.startswith("http://") or redirect_host.startswith("https://"):
                            redirected_base = redirect_host
                        else:
                            redirected_base = f"https://{redirect_host}"
                        if redirected_base != current_base_url:
                            current_base_url = redirected_base

                elif status == "expired":
                    refresh_count += 1
                    if refresh_count > MAX_QR_REFRESH_COUNT:
                        logger.warning(
                            "WechatChannel: QR expired {} times, giving up",
                            refresh_count - 1,
                        )
                        return False
                    qrcode_id, scan_url = await self._fetch_qr_code()
                    current_base_url = self._base_url()
                    self._print_qr_code(scan_url)

                await asyncio.sleep(1)

        except Exception:
            logger.exception("WechatChannel: QR login failed")
            return False

    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    def _pause_session(self, duration_s: int = SESSION_PAUSE_DURATION_S) -> None:
        self._session_pause_until = time.time() + duration_s

    def _session_pause_remaining_s(self) -> int:
        remaining = int(self._session_pause_until - time.time())
        if remaining <= 0:
            self._session_pause_until = 0.0
            return 0
        return remaining

    async def _poll_loop(self) -> None:
        """Main long-poll loop.  Runs until ``_stop_event`` is set."""
        logger.info("WechatChannel: poll loop started")
        consecutive_failures = 0

        while self._stop_event is not None and not self._stop_event.is_set():
            remaining = self._session_pause_remaining_s()
            if remaining > 0:
                await asyncio.sleep(min(remaining, 30))
                continue

            try:
                if self._client is not None:
                    self._client.timeout = httpx.Timeout(self._next_poll_timeout_s + 10, connect=30)

                await self._poll_once()
                consecutive_failures = 0
            except httpx.TimeoutException:
                continue
            except Exception:
                if self._stop_event is not None and self._stop_event.is_set():
                    break
                logger.opt(exception=True).warning("WechatChannel: poll error")
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    consecutive_failures = 0
                    await asyncio.sleep(BACKOFF_DELAY_S)
                else:
                    await asyncio.sleep(RETRY_DELAY_S)

    async def _poll_once(self) -> None:
        """Single getupdates request and message processing."""
        body: dict[str, Any] = {
            "get_updates_buf": self._get_updates_buf,
            "base_info": BASE_INFO,
        }

        data = await self._api_post("ilink/bot/getupdates", body)

        ret = data.get("ret", 0)
        errcode = data.get("errcode", 0)
        is_error = (ret is not None and ret != 0) or (errcode is not None and errcode != 0)

        if is_error:
            if errcode == ERRCODE_SESSION_EXPIRED or ret == ERRCODE_SESSION_EXPIRED:
                self._pause_session()
                remaining = self._session_pause_remaining_s()
                logger.warning(
                    "WechatChannel: session expired (errcode {}), pausing {} min",
                    errcode,
                    max((remaining + 59) // 60, 1),
                )
                return
            raise RuntimeError(
                f"getUpdates failed: ret={ret} errcode={errcode} errmsg={data.get('errmsg', '')}"
            )

        server_timeout_ms = data.get("longpolling_timeout_ms")
        if server_timeout_ms and server_timeout_ms > 0:
            self._next_poll_timeout_s = max(server_timeout_ms // 1000, 5)

        new_buf = data.get("get_updates_buf", "")
        if new_buf:
            self._get_updates_buf = new_buf
            self._persist_state()

        msgs: list[dict] = data.get("msgs", []) or []
        for raw_msg in msgs:
            try:
                await self._on_message(raw_msg)
            except Exception:
                logger.opt(exception=True).warning("WechatChannel: message processing error")

    # ------------------------------------------------------------------
    # Inbound message processing
    # ------------------------------------------------------------------

    async def _on_message(self, raw_msg: dict) -> None:
        """Process a single raw message from getupdates."""
        msg = self._parse(raw_msg, allowed_from=None)
        if msg is None:
            return

        msg_id = raw_msg.get("message_id") or raw_msg.get(
            "seq"
        ) or f"{raw_msg.get('from_user_id', '')}_{raw_msg.get('create_time_ms', '')}"
        msg_id = str(msg_id)
        if msg_id in self._processed_ids:
            return

        self._processed_ids[msg_id] = None
        while len(self._processed_ids) > 1000:
            self._processed_ids.popitem(last=False)

        ctx_token = raw_msg.get("context_token", "")
        if ctx_token:
            self._context_tokens[msg.user_id] = ctx_token
            self._persist_state(force=True)

        logger.info(
            "WechatChannel: inbound from={} bodyLen={}",
            msg.user_id,
            len(msg.text),
        )

        self._chat_ids[msg.session_key] = msg.chat_id
        await self._bus.inbound(msg.session_key).put(InboundMessage(
            session_key=msg.session_key,
            content=msg.text,
            source=_PLATFORM,
            correlation_id=msg.session_key,
        ))

        if msg.session_key not in self._serve_tasks:
            self._serve_tasks[msg.session_key] = asyncio.create_task(
                self._orchestrator.serve(self._bus, msg.session_key)
            )

    async def _deliver_scheduled(self, task) -> None:
        """Push a scheduled task prompt onto the bus for the target session."""
        await self._bus.inbound(task.session_key).put(InboundMessage(
            session_key=task.session_key,
            content=task.prompt,
            source=_PLATFORM,
            correlation_id=task.session_key,
        ))
        if task.session_key not in self._serve_tasks:
            self._serve_tasks[task.session_key] = asyncio.create_task(
                self._orchestrator.serve(self._bus, task.session_key)
            )

    # ------------------------------------------------------------------
    # Message parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse(
        raw_msg: dict, *, allowed_from: list[str] | None = None
    ) -> ChannelMessage | None:
        """Parse an iLink getupdates message dict into a normalized ChannelMessage.

        Returns ``None`` if the message should be skipped (bot message, empty
        content, blocked sender, etc.).
        """
        if raw_msg.get("message_type") == MESSAGE_TYPE_BOT:
            return None

        from_user_id = raw_msg.get("from_user_id", "") or ""
        if not from_user_id:
            return None

        if allowed_from and from_user_id not in allowed_from:
            return None

        item_list: list[dict] = raw_msg.get("item_list") or []
        content_parts: list[str] = []

        for item in item_list:
            item_type = item.get("type", 0)
            if item_type == ITEM_TEXT:
                text = (item.get("text_item") or {}).get("text", "")
                if not text:
                    continue
                ref = item.get("ref_msg")
                if ref:
                    ref_item = ref.get("message_item")
                    if ref_item and ref_item.get("type", 0) in (
                        ITEM_IMAGE, ITEM_VOICE, ITEM_FILE, ITEM_VIDEO,
                    ):
                        content_parts.append(text)
                    else:
                        parts: list[str] = []
                        if ref.get("title"):
                            parts.append(ref["title"])
                        if ref_item:
                            ref_text = (ref_item.get("text_item") or {}).get("text", "")
                            if ref_text:
                                parts.append(ref_text)
                        if parts:
                            content_parts.append(f"[引用: {' | '.join(parts)}]\n{text}")
                        else:
                            content_parts.append(text)
                else:
                    content_parts.append(text)

        content = "\n".join(content_parts)
        if not content:
            return None

        session_key = BaseChannel.build_session_key(_PLATFORM, "private", from_user_id)
        return ChannelMessage(
            session_key=session_key,
            text=content,
            user_id=from_user_id,
            chat_id=from_user_id,
            chat_type="private",
            platform=_PLATFORM,
            raw=raw_msg,
        )

    # ------------------------------------------------------------------
    # Outbound consumer
    # ------------------------------------------------------------------

    async def _consume_outbound(self) -> None:
        """Read from ``bus.outbound("wechat")`` and send replies via iLink."""
        while self._stop_event is None or not self._stop_event.is_set():
            try:
                out = await asyncio.wait_for(
                    self._bus.outbound(_PLATFORM).get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue
            if out is None:
                continue
            if out.msg_type != "final":
                continue

            chat_id = self._chat_ids.get(out.session_key)
            if chat_id is None:
                logger.warning(
                    "WechatChannel: no chat_id for session {!r}, dropping reply",
                    out.session_key,
                )
                continue

            data = out.data or {}
            text = data.get("content", "")
            if not text:
                continue

            ctx_token = self._context_tokens.get(chat_id, "")
            if not ctx_token:
                await self._refresh_context_tokens()
                ctx_token = self._context_tokens.get(chat_id, "")
            if not ctx_token:
                logger.error(
                    "WechatChannel: no context_token for {}, dropping reply", chat_id
                )
                continue

            chunks = _split_text(text, WEIXIN_MAX_MESSAGE_LEN)
            for chunk in chunks:
                try:
                    await self._send_text(chat_id, chunk, ctx_token)
                except Exception:
                    logger.opt(exception=True).warning(
                        "WechatChannel: failed to send to {}", chat_id
                    )

    # ------------------------------------------------------------------
    # Send text via iLink API
    # ------------------------------------------------------------------

    async def _send_text(
        self,
        to_user_id: str,
        text: str,
        context_token: str,
    ) -> None:
        """Send a text message via iLink ``sendmessage`` API."""
        client_id = f"mybot-{uuid.uuid4().hex[:12]}"

        item_list: list[dict] = []
        if text:
            item_list.append({"type": ITEM_TEXT, "text_item": {"text": text}})

        wechat_msg: dict[str, Any] = {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": client_id,
            "message_type": MESSAGE_TYPE_BOT,
            "message_state": MESSAGE_STATE_FINISH,
        }
        if item_list:
            wechat_msg["item_list"] = item_list
        if context_token:
            wechat_msg["context_token"] = context_token

        body: dict[str, Any] = {
            "msg": wechat_msg,
            "base_info": BASE_INFO,
        }

        data = await self._api_post("ilink/bot/sendmessage", body)
        ret = data.get("ret", 0)
        errcode = data.get("errcode", 0)
        if (ret is not None and ret != 0) or (errcode is not None and errcode != 0):
            raise RuntimeError(
                f"WeChat send error (ret={ret}, errcode={errcode}): {data.get('errmsg', '')}"
            )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Entry point for ``mybot-wechat``."""
    from pathlib import Path

    from config import Config

    orchestrator = build_orchestrator()

    workspace = Path(Config.workspace).expanduser()
    workspace.mkdir(parents=True, exist_ok=True)

    bot = WechatChannel(orchestrator)

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGINT, lambda: asyncio.ensure_future(bot.shutdown()))
        loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.ensure_future(bot.shutdown()))
        await bot.start()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("WechatChannel: exited")
