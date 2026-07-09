"""WeChat personal bot channel via itchat-uos.

Provides a ``mybot-wechat`` entry point that logs into a personal WeChat
account via QR code and bridges messages to the async Orchestrator.

Messages flow through the MessageBus for decoupled I/O:
  itchat thread → InboundMessage("wechat") → Orchestrator.serve()
  → OutboundMessage("wechat") → consumer → itchat.send()

Usage::

    pip install -e ".[wechat]"
    mybot-wechat
"""

from __future__ import annotations

import asyncio
import re
import signal
from pathlib import Path

from loguru import logger

from channels.base import BaseChannel, ChannelMessage, build_orchestrator
from core.message_bus import InboundMessage, MessageBus

_WECHAT = "wechat"
_MAX_MSG_CHARS = 2000
from config import Config

_FALLBACK_CHAT = Config.xiaohongshu_fallback_chat


class WeChatBot(BaseChannel):
    """Bridges itchat-uos message handlers to the async Orchestrator via MessageBus.

    itchat-uos is synchronous and runs a dispatch loop in a daemon thread.
    Handlers parse raw messages synchronously and enqueue
    :class:`InboundMessage` on the MessageBus via
    ``run_coroutine_threadsafe``.  A consumer task reads
    ``bus.outbound("wechat")`` and sends replies through itchat.
    """

    channel_name = "wechat"

    def __init__(self, orchestrator) -> None:
        super().__init__(orchestrator)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._bus = MessageBus()
        self._chat_ids: dict[str, str] = {}  # session_key → chat_id
        self._serve_tasks: dict[str, asyncio.Task] = {}
        self._consumer_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # BaseChannel contract
    # ------------------------------------------------------------------

    async def start(self) -> None:
        logger.info("WeChatBot: starting background services")
        self._orchestrator.scheduled_tasks.set_deliver(self._deliver_scheduled)
        publish_tool = self._orchestrator.get_tool("xiaohongshu_publish")
        if publish_tool is not None:
            publish_tool.set_notify(self._notify_publish_fallback)
        await self._orchestrator.start_services()

        self._register_handlers()

        import itchat
        itchat.run(blockThread=False)
        logger.info("WeChatBot: itchat dispatch started, listening for messages")

        self._stop_event = asyncio.Event()
        self._consumer_task = asyncio.create_task(self._consume_outbound())
        await self._stop_event.wait()

    async def shutdown(self) -> None:
        logger.info("WeChatBot: shutting down")
        import itchat
        itchat.alive = False
        if self._stop_event is not None and not self._stop_event.is_set():
            self._stop_event.set()
        for task in self._serve_tasks.values():
            task.cancel()
        if self._consumer_task is not None:
            self._consumer_task.cancel()
        await self._bus.close()

    async def send_reply(self, text: str, msg: ChannelMessage) -> None:
        import itchat

        chunks = _split_text(text, _MAX_MSG_CHARS)
        loop = asyncio.get_running_loop()
        for chunk in chunks:
            try:
                await loop.run_in_executor(
                    None, lambda c=chunk: itchat.send(c, msg.chat_id)
                )
            except Exception:
                logger.opt(exception=True).warning(
                    "WeChatBot: failed to send msg to {}", msg.chat_id
                )

    # ------------------------------------------------------------------
    # Handler registration (itchat-specific)
    # ------------------------------------------------------------------

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def _register_handlers(self) -> None:
        import itchat

        @itchat.msg_register(itchat.content.TEXT, isFriendChat=True)
        def _on_private(msg):
            self._on_message(msg, is_group=False)
            return None

        @itchat.msg_register(itchat.content.TEXT, isGroupChat=True)
        def _on_group(msg):
            if msg.get("IsAt"):
                self._on_message(msg, is_group=True)
            return None

    # ------------------------------------------------------------------
    # Sync handler (runs in itchat daemon thread)
    # ------------------------------------------------------------------

    def _on_message(self, raw_msg: dict, *, is_group: bool) -> None:
        """Parse raw msg and enqueue on the MessageBus from itchat's thread."""
        if self._loop is None or not self._loop.is_running():
            logger.warning("WeChatBot: event loop not running, dropping msg")
            return
        msg = self._parse(raw_msg, is_group=is_group)
        if not msg.text:
            return
        asyncio.run_coroutine_threadsafe(
            self._enqueue(msg), self._loop
        )

    async def _enqueue(self, msg: ChannelMessage) -> None:
        """Put *msg* on the inbound queue and ensure a serve task runs for this session."""
        self._chat_ids[msg.session_key] = msg.chat_id
        await self._bus.inbound(msg.session_key).put(InboundMessage(
            session_key=msg.session_key,
            content=msg.text,
            source=_WECHAT,
            correlation_id=msg.session_key,
        ))
        if msg.session_key not in self._serve_tasks:
            self._serve_tasks[msg.session_key] = asyncio.create_task(
                self._orchestrator.serve(self._bus, msg.session_key)
            )

    async def _deliver_scheduled(self, task) -> None:
        """Push a scheduled task's result to the user by injecting its prompt on the bus.

        The prompt runs through the normal serve → outbound("wechat") → consumer
        pipeline, so the result reaches whichever chat the session was last seen in.
        If the session has no known chat_id (e.g. bot restarted, user hasn't messaged
        since), the outbound consumer drops the reply with a warning.
        """
        await self._bus.inbound(task.session_key).put(InboundMessage(
            session_key=task.session_key,
            content=task.prompt,
            source=_WECHAT,
            correlation_id=task.session_key,
        ))
        if task.session_key not in self._serve_tasks:
            self._serve_tasks[task.session_key] = asyncio.create_task(
                self._orchestrator.serve(self._bus, task.session_key)
            )

    # ------------------------------------------------------------------
    # Outbound consumer
    # ------------------------------------------------------------------

    async def _consume_outbound(self) -> None:
        """Read from bus.outbound("wechat") and send replies via itchat."""
        while self._stop_event is None or not self._stop_event.is_set():
            try:
                out = await asyncio.wait_for(
                    self._bus.outbound(_WECHAT).get(), timeout=1.0
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
                    "WeChatBot: no chat_id for session {!r}, dropping reply",
                    out.session_key,
                )
                continue
            data = out.data or {}
            text = data.get("content", "")
            if text:
                await self._send_text(text, chat_id)

    async def _send_text(self, text: str, chat_id: str) -> None:
        """Send text (split into chunks if necessary) via itchat."""
        import itchat

        chunks = _split_text(text, _MAX_MSG_CHARS)
        loop = asyncio.get_running_loop()
        for chunk in chunks:
            try:
                await loop.run_in_executor(
                    None, lambda c=chunk: itchat.send(c, chat_id)
                )
            except Exception:
                logger.opt(exception=True).warning(
                    "WeChatBot: failed to send msg to {}", chat_id
                )

    async def _notify_publish_fallback(self, draft: dict) -> None:
        """Push a failed auto-publish draft (caption + cover image) to the operator.

        The headless auto-publish fills the note but cannot reliably click
        Xiaohongshu's closed-shadow submit button; on that "unconfirmed"
        outcome the rendered card + caption are sent to the WeChat file-transfer
        helper (``filehelper`` by default, override via
        ``XIAOHONGSHU_FALLBACK_CHAT``) so the operator can post it manually.
        """
        import itchat

        title = draft.get("title", "")
        caption = draft.get("caption") or draft.get("content", "")
        image = draft.get("image", "")
        text = (
            "⚠️ 小红书自动发布未确认，请手动发布：\n\n"
            f"【{title}】\n\n{caption}"
        )
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None, lambda: itchat.send(text, _FALLBACK_CHAT)
            )
            if image and Path(image).exists():
                await loop.run_in_executor(
                    None, lambda: itchat.send_image(image, _FALLBACK_CHAT)
                )
        except Exception:
            logger.opt(exception=True).warning(
                "WeChatBot: failed to push xhs publish fallback"
            )

    @staticmethod
    def _parse(raw_msg: dict, *, is_group: bool) -> ChannelMessage:
        """Convert a raw itchat msg dict to a normalized ChannelMessage."""
        to_user = raw_msg["FromUserName"]
        if is_group:
            chat_type = "group"
            session_key = BaseChannel.build_session_key(_WECHAT, chat_type, to_user)
            text = raw_msg.get("Text", "")
            text = re.sub(r"^@.+?[ \s]", "", text, count=1).strip()
        else:
            chat_type = "private"
            session_key = BaseChannel.build_session_key(_WECHAT, chat_type, to_user)
            text = raw_msg.get("Text", "").strip()
        return ChannelMessage(
            session_key=session_key,
            text=text,
            user_id=raw_msg.get("FromUserName", ""),
            chat_id=to_user,
            chat_type=chat_type,
            platform=_WECHAT,
            raw=raw_msg,
        )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _split_text(text: str, max_chars: int) -> list[str]:
    """Split a long message into chunks that fit in a single WeChat message."""
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


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------


def main() -> None:
    """Entry point for ``mybot-wechat``."""
    from pathlib import Path

    import itchat as _itchat

    from config import Config

    orchestrator = build_orchestrator()

    workspace = Path(Config.workspace).expanduser()
    workspace.mkdir(parents=True, exist_ok=True)
    pkl_path = str(workspace / "itchat.pkl")
    print("Logging into WeChat — scan the QR code below...")
    _itchat.auto_login(hotReload=True, statusStorageDir=pkl_path, enableCmdQR=2)

    bot = WeChatBot(orchestrator)

    async def _run() -> None:
        bot.set_loop(asyncio.get_running_loop())
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGINT, bot.shutdown)
        loop.add_signal_handler(signal.SIGTERM, bot.shutdown)
        await bot.start()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("WeChatBot: cleaning up")
        _itchat.logout()
