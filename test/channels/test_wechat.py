"""Tests for channels/ — base abstractions + WeChatBot."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from channels.base import BaseChannel, ChannelMessage, build_orchestrator
from channels.wechat import _MAX_MSG_CHARS, WeChatBot, _split_text
from core.message_bus import OutboundMessage

# =========================================================================
# ChannelMessage
# =========================================================================


class TestChannelMessage:
    def test_defaults(self):
        msg = ChannelMessage(
            session_key="test:private:u1",
            text="hello",
            user_id="u1",
            chat_id="u1",
            chat_type="private",
            platform="test",
        )
        assert msg.text == "hello"
        assert msg.raw == {}

    def test_raw_preserved(self):
        raw = {"FromUserName": "@x", "Text": "hi", "CreateTime": 123}
        msg = ChannelMessage(
            session_key="wx:private:@x",
            text="hi",
            user_id="@x",
            chat_id="@x",
            chat_type="private",
            platform="wechat",
            raw=raw,
        )
        assert msg.raw is raw
        assert msg.raw["CreateTime"] == 123


# =========================================================================
# BaseChannel.build_session_key
# =========================================================================


class TestBuildSessionKey:
    def test_private(self):
        key = BaseChannel.build_session_key("wechat", "private", "@abc")
        assert key == "wechat:private:@abc"

    def test_group(self):
        key = BaseChannel.build_session_key("qq", "group", "room123")
        assert key == "qq:group:room123"

    def test_namespaces_per_platform(self):
        k1 = BaseChannel.build_session_key("wechat", "private", "u1")
        k2 = BaseChannel.build_session_key("qq", "private", "u1")
        assert k1 != k2


# =========================================================================
# build_orchestrator
# =========================================================================


class TestBuildOrchestrator:
    def test_returns_orchestrator(self):
        orch = build_orchestrator()
        from core.orchestrator import Orchestrator
        assert isinstance(orch, Orchestrator)


# =========================================================================
# WeChatBot._parse
# =========================================================================


class TestWeChatParse:
    def test_private_chat(self):
        raw = {"FromUserName": "@abc123", "Text": "hello world"}
        msg = WeChatBot._parse(raw, is_group=False)
        assert isinstance(msg, ChannelMessage)
        assert msg.session_key == "wechat:private:@abc123"
        assert msg.text == "hello world"
        assert msg.chat_type == "private"
        assert msg.platform == "wechat"

    def test_private_strips_whitespace(self):
        raw = {"FromUserName": "@xyz", "Text": "  hi there  "}
        msg = WeChatBot._parse(raw, is_group=False)
        assert msg.text == "hi there"

    def test_group_chat_strips_mention(self):
        raw = {"FromUserName": "@@room456", "Text": "@bot what is python"}
        msg = WeChatBot._parse(raw, is_group=True)
        assert msg.session_key == "wechat:group:@@room456"
        assert msg.text == "what is python"
        assert msg.chat_type == "group"

    def test_empty_text(self):
        raw = {"FromUserName": "@abc", "Text": ""}
        msg = WeChatBot._parse(raw, is_group=False)
        assert msg.text == ""

    def test_session_keys_isolate_users(self):
        m1 = WeChatBot._parse({"FromUserName": "@alice", "Text": "hi"}, is_group=False)
        m2 = WeChatBot._parse({"FromUserName": "@bob", "Text": "hi"}, is_group=False)
        assert m1.session_key != m2.session_key

    def test_raw_preserved(self):
        raw = {"FromUserName": "@x", "Text": "hi", "ExtraField": 42}
        msg = WeChatBot._parse(raw, is_group=False)
        assert msg.raw is raw


# =========================================================================
# _split_text
# =========================================================================


class TestSplitText:
    def test_short_message_single_chunk(self):
        assert _split_text("Hello", _MAX_MSG_CHARS) == ["Hello"]

    def test_exactly_max_chars(self):
        text = "x" * _MAX_MSG_CHARS
        chunks = _split_text(text, _MAX_MSG_CHARS)
        assert len(chunks) == 1

    def test_long_splits_at_newline(self):
        filler = "x" * 1500
        text = "line one\n" + filler + "line two\n" + filler + "final"
        chunks = _split_text(text, _MAX_MSG_CHARS)
        assert len(chunks) >= 2

    def test_long_no_newlines_splits_at_max(self):
        text = "x" * (_MAX_MSG_CHARS + 500)
        chunks = _split_text(text, _MAX_MSG_CHARS)
        assert len(chunks) == 2
        assert len(chunks[0]) == _MAX_MSG_CHARS
        assert len(chunks[1]) == 500


# =========================================================================
# WeChatBot integration (mocked) — MessageBus-based
# =========================================================================


class TestWeChatBotIntegration:
    @pytest.fixture
    def mock_orchestrator(self):
        orch = MagicMock()
        orch.serve = AsyncMock()
        orch.start_services = AsyncMock()
        return orch

    @pytest.fixture
    def bot(self, mock_orchestrator):
        bot = WeChatBot(mock_orchestrator)
        try:
            bot._loop = asyncio.get_running_loop()
        except RuntimeError:
            bot._loop = asyncio.new_event_loop()
        return bot

    @pytest.mark.asyncio
    async def test_enqueue_puts_inbound_message(self, bot, mock_orchestrator):
        msg = ChannelMessage(
            session_key="wechat:private:@user123",
            text="hello",
            user_id="@user123",
            chat_id="@user123",
            chat_type="private",
            platform="wechat",
        )
        await bot._enqueue(msg)

        inbound = await bot._bus.inbound("wechat:private:@user123").get()
        assert inbound.session_key == "wechat:private:@user123"
        assert inbound.content == "hello"
        assert inbound.source == "wechat"

    @pytest.mark.asyncio
    async def test_enqueue_starts_serve_task(self, bot, mock_orchestrator):
        msg = ChannelMessage(
            session_key="wechat:private:@user123",
            text="hello",
            user_id="@user123",
            chat_id="@user123",
            chat_type="private",
            platform="wechat",
        )
        await bot._enqueue(msg)

        mock_orchestrator.serve.assert_called_once()
        args = mock_orchestrator.serve.call_args.args
        assert args[1] == "wechat:private:@user123"

    @pytest.mark.asyncio
    async def test_enqueue_stores_chat_id(self, bot):
        msg = ChannelMessage(
            session_key="wechat:private:@user123",
            text="hello",
            user_id="@user123",
            chat_id="@user123",
            chat_type="private",
            platform="wechat",
        )
        await bot._enqueue(msg)
        assert bot._chat_ids["wechat:private:@user123"] == "@user123"

    @pytest.mark.asyncio
    async def test_enqueue_reuses_serve_task(self, bot, mock_orchestrator):
        msg = ChannelMessage(
            session_key="wechat:private:@u1", text="msg1",
            user_id="@u1", chat_id="@u1", chat_type="private", platform="wechat",
        )
        await bot._enqueue(msg)
        await bot._enqueue(msg)
        assert mock_orchestrator.serve.call_count == 1

    @pytest.mark.asyncio
    async def test_group_message_session_key(self, bot, mock_orchestrator):
        msg = ChannelMessage(
            session_key="wechat:group:@@group789",
            text="do something",
            user_id="@@group789",
            chat_id="@@group789",
            chat_type="group",
            platform="wechat",
        )
        await bot._enqueue(msg)

        inbound = await bot._bus.inbound("wechat:group:@@group789").get()
        assert inbound.source == "wechat"

    @pytest.mark.asyncio
    async def test_shutdown_cleans_up(self, bot):
        bot._stop_event = asyncio.Event()
        bot._consumer_task = asyncio.create_task(asyncio.sleep(0))
        await bot.shutdown()
        assert bot._stop_event.is_set()

    def test_set_loop(self, bot):
        loop = MagicMock()
        bot.set_loop(loop)
        assert bot._loop is loop

    def test_channel_name(self, bot):
        assert bot.channel_name == "wechat"

    @pytest.mark.asyncio
    async def test_consume_outbound_sends_reply(self, bot):
        """Consumer reads final message from outbound queue and sends via itchat."""
        bot._chat_ids["wechat:private:@user123"] = "@user123"
        bot._stop_event = asyncio.Event()

        await bot._bus.outbound("wechat").put(OutboundMessage(
            session_key="wechat:private:@user123",
            correlation_id="wechat:private:@user123",
            msg_type="final",
            data={"content": "hello from bot"},
        ))

        with patch.object(bot, "_send_text", new_callable=AsyncMock) as mock_send:
            consumer = asyncio.create_task(bot._consume_outbound())
            await asyncio.sleep(0.15)
            bot._stop_event.set()
            await consumer

            mock_send.assert_called_once_with("hello from bot", "@user123")

    @pytest.mark.asyncio
    async def test_consume_outbound_ignores_non_final(self, bot):
        """Consumer ignores delta/thinking messages, only sends finals."""
        bot._chat_ids["wechat:private:@user123"] = "@user123"
        bot._stop_event = asyncio.Event()

        await bot._bus.outbound("wechat").put(OutboundMessage(
            session_key="wechat:private:@user123",
            correlation_id="cid", msg_type="delta", data="streaming...",
        ))
        await bot._bus.outbound("wechat").put(OutboundMessage(
            session_key="wechat:private:@user123",
            correlation_id="cid", msg_type="final",
            data={"content": "final answer"},
        ))

        with patch.object(bot, "_send_text", new_callable=AsyncMock) as mock_send:
            consumer = asyncio.create_task(bot._consume_outbound())
            await asyncio.sleep(0.2)
            bot._stop_event.set()
            await consumer

            assert mock_send.call_count == 1
            mock_send.assert_called_with("final answer", "@user123")


class TestHandlerRegistration:
    @patch("itchat.msg_register")
    def test_register_handlers_calls_msg_register(self, mock_register):
        bot = WeChatBot(MagicMock())
        mock_register.return_value = lambda f: f
        bot._register_handlers()
        assert mock_register.call_count == 2


class TestXhsPublishFallback:
    async def test_pushes_caption_and_image_to_filehelper(self, tmp_path):
        bot = WeChatBot(MagicMock())
        img = tmp_path / "cover.png"
        img.write_bytes(b"\x89PNG")
        with (
            patch("itchat.send", new=MagicMock()) as send,
            patch("itchat.send_image", new=MagicMock(), create=True) as send_image,
        ):
            await bot._notify_publish_fallback({
                "title": "海龟汤答案",
                "content": "正文",
                "caption": "答案见图 #海龟汤",
                "image": str(img),
            })

        assert send.called
        assert send.call_args.args[1] == "filehelper"
        assert "海龟汤答案" in send.call_args.args[0]
        assert send_image.called
        assert send_image.call_args.args[1] == "filehelper"

    async def test_missing_image_skips_image_send(self):
        bot = WeChatBot(MagicMock())
        with (
            patch("itchat.send", new=MagicMock()) as send,
            patch("itchat.send_image", new=MagicMock(), create=True) as send_image,
        ):
            await bot._notify_publish_fallback({
                "title": "t", "content": "c", "caption": "", "image": "",
            })

        assert send.called
        assert not send_image.called
