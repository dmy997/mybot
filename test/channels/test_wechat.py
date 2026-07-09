"""Tests for channels/wechat.py — iLink personal WeChat bot channel."""

from __future__ import annotations

import asyncio
import json
from collections import OrderedDict
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from channels.base import ChannelMessage
from channels.wechat import (
    APP_ID,
    BASE_URL,
    ERRCODE_SESSION_EXPIRED,
    ITEM_TEXT,
    MESSAGE_STATE_FINISH,
    MESSAGE_TYPE_BOT,
    WEIXIN_MAX_MESSAGE_LEN,
    WechatChannel,
    _build_client_version,
    _client_version,
    _make_headers,
    _random_wechat_uin,
    _split_text,
)

# =========================================================================
# Protocol helpers (pure functions)
# =========================================================================


class TestClientVersion:
    def test_semver_encoding(self):
        v = _client_version("2.1.1")
        assert v == (2 << 16) | (1 << 8) | 1

    def test_alias(self):
        assert _client_version is _build_client_version

    def test_partial_version(self):
        v = _client_version("1.0")
        assert v == (1 << 16) | (0 << 8) | 0

    def test_extra_segments_ignored(self):
        v = _client_version("3.2.1.999")
        assert v == (3 << 16) | (2 << 8) | 1


class TestRandomWechatUin:
    def test_returns_base64_string(self):
        result = _random_wechat_uin()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_is_valid_base64(self):
        import re
        result = _random_wechat_uin()
        assert re.fullmatch(r"[A-Za-z0-9+/]+=*", result)

    def test_unique_calls(self):
        results = {_random_wechat_uin() for _ in range(10)}
        assert len(results) > 1


class TestMakeHeaders:
    def test_without_auth(self):
        h = _make_headers(token="", auth=False)
        assert h["Content-Type"] == "application/json"
        assert h["AuthorizationType"] == "ilink_bot_token"
        assert h["iLink-App-Id"] == APP_ID
        assert "Authorization" not in h

    def test_with_auth(self):
        h = _make_headers(token="test-token", auth=True)
        assert h["Authorization"] == "Bearer test-token"

    def test_uin_is_fresh_each_call(self):
        h1 = _make_headers(token="t", auth=True)
        h2 = _make_headers(token="t", auth=True)
        assert h1["X-WECHAT-UIN"] != h2["X-WECHAT-UIN"]


# =========================================================================
# Message parsing (static method)
# =========================================================================


class TestWeixinParse:
    """Parse iLink getupdates message dict → ChannelMessage."""

    def test_text_message(self):
        raw = {
            "message_id": "msg_001",
            "from_user_id": "@user_abc",
            "context_token": "ctx_123",
            "item_list": [
                {"type": ITEM_TEXT, "text_item": {"text": "hello world"}},
            ],
        }
        msg = WechatChannel._parse(raw, allowed_from=None)
        assert msg is not None
        assert msg.session_key == "wechat:private:@user_abc"
        assert msg.text == "hello world"
        assert msg.user_id == "@user_abc"
        assert msg.chat_id == "@user_abc"
        assert msg.chat_type == "private"
        assert msg.platform == "wechat"

    def test_multi_text_items(self):
        raw = {
            "message_id": "msg_002",
            "from_user_id": "@user_abc",
            "item_list": [
                {"type": ITEM_TEXT, "text_item": {"text": "part 1"}},
                {"type": ITEM_TEXT, "text_item": {"text": "part 2"}},
            ],
        }
        msg = WechatChannel._parse(raw, allowed_from=None)
        assert msg.text == "part 1\npart 2"

    def test_empty_item_list(self):
        raw = {
            "message_id": "msg_003",
            "from_user_id": "@user_abc",
            "item_list": [],
        }
        msg = WechatChannel._parse(raw, allowed_from=None)
        assert msg is None

    def test_no_from_user_id(self):
        raw = {
            "message_id": "msg_004",
            "from_user_id": "",
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": "hi"}}],
        }
        msg = WechatChannel._parse(raw, allowed_from=None)
        assert msg is None

    def test_bot_message_skipped(self):
        raw = {
            "message_id": "msg_005",
            "message_type": MESSAGE_TYPE_BOT,
            "from_user_id": "@user_abc",
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": "bot reply"}}],
        }
        msg = WechatChannel._parse(raw, allowed_from=None)
        assert msg is None

    def test_allowed_from_filter(self):
        raw = {
            "message_id": "msg_006",
            "from_user_id": "@stranger",
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": "hi"}}],
        }
        msg = WechatChannel._parse(raw, allowed_from=["@friend"])
        assert msg is None

    def test_allowed_from_allows(self):
        raw = {
            "message_id": "msg_007",
            "from_user_id": "@friend",
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": "hi"}}],
        }
        msg = WechatChannel._parse(raw, allowed_from=["@friend"])
        assert msg is not None
        assert msg.text == "hi"

    def test_fallback_message_id(self):
        raw = {
            "from_user_id": "@user_abc",
            "create_time_ms": 1234567890,
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": "hi"}}],
        }
        msg = WechatChannel._parse(raw, allowed_from=None)
        assert msg is not None

    def test_ref_msg_inline(self):
        raw = {
            "message_id": "msg_008",
            "from_user_id": "@user_abc",
            "item_list": [{
                "type": ITEM_TEXT,
                "text_item": {"text": "reply"},
                "ref_msg": {
                    "title": "Original",
                    "message_item": {
                        "type": ITEM_TEXT,
                        "text_item": {"text": "quoted text"},
                    },
                },
            }],
        }
        msg = WechatChannel._parse(raw, allowed_from=None)
        assert "[引用:" in msg.text
        assert "reply" in msg.text

    def test_raw_preserved(self):
        raw = {
            "message_id": "msg_009",
            "from_user_id": "@user_abc",
            "extra_field": 42,
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": "hi"}}],
        }
        msg = WechatChannel._parse(raw, allowed_from=None)
        assert msg.raw is raw


# =========================================================================
# State persistence
# =========================================================================


class TestStatePersistence:
    @pytest.fixture
    def state_dir(self, tmp_path):
        d = tmp_path / "wechat_state"
        d.mkdir()
        return d

    def test_save_and_load_roundtrip(self, state_dir):
        token = "test-bot-token-123"
        buf = "cursor_abc"
        ctx_tokens = {"@user1": "ctx_001"}
        base_url = "https://custom.example.com"

        WechatChannel._save_state(
            state_dir, token=token, get_updates_buf=buf,
            context_tokens=ctx_tokens, base_url=base_url,
        )
        loaded = WechatChannel._load_state(state_dir)
        assert loaded["token"] == token
        assert loaded["get_updates_buf"] == buf
        assert loaded["context_tokens"] == ctx_tokens
        assert loaded["base_url"] == base_url

    def test_load_nonexistent_file(self, state_dir):
        loaded = WechatChannel._load_state(state_dir)
        assert loaded["token"] == ""
        assert loaded["context_tokens"] == {}

    def test_load_corrupt_file(self, state_dir):
        f = state_dir / "account.json"
        f.write_text("{not valid json")
        loaded = WechatChannel._load_state(state_dir)
        assert loaded["token"] == ""

    def test_save_creates_dir(self, tmp_path):
        d = tmp_path / "new_wechat_state"
        WechatChannel._save_state(d, token="tk", get_updates_buf="",
                                   context_tokens={}, base_url=BASE_URL)
        assert (d / "account.json").exists()


# =========================================================================
# API helpers (mocked HTTP)
# =========================================================================


class TestApiHelpers:
    @pytest.fixture
    def chan(self, mock_orchestrator):
        c = WechatChannel(mock_orchestrator)
        c._token = "test-token"
        c._client = MagicMock(spec=httpx.AsyncClient)
        return c

    @pytest.mark.asyncio
    async def test_api_get(self, chan):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        chan._client.get = AsyncMock(return_value=mock_resp)

        result = await chan._api_get("test/endpoint", params={"k": "v"})
        assert result == {"ok": True}
        chan._client.get.assert_called_once()
        call_url = chan._client.get.call_args.args[0]
        assert "test/endpoint" in call_url

    @pytest.mark.asyncio
    async def test_api_post(self, chan):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        chan._client.post = AsyncMock(return_value=mock_resp)

        result = await chan._api_post("test/endpoint", {"key": "val"})
        assert result == {"ok": True}
        chan._client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_api_post_injects_base_info(self, chan):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        chan._client.post = AsyncMock(return_value=mock_resp)

        await chan._api_post("test/endpoint", {"custom": "data"})
        call_body = chan._client.post.call_args.kwargs["json"]
        assert call_body["custom"] == "data"
        assert "base_info" in call_body


# =========================================================================
# QR code login
# =========================================================================


class TestQrLogin:
    @pytest.fixture
    def chan(self, mock_orchestrator):
        c = WechatChannel(mock_orchestrator)
        c._token = ""
        mock_client = MagicMock(spec=httpx.AsyncClient)
        c._client = mock_client
        return c

    @pytest.mark.asyncio
    async def test_fetch_qr_code(self, chan):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "qrcode": "qr_id_123",
            "qrcode_img_content": "https://scan.example.com/qr",
        }
        chan._client.get = AsyncMock(return_value=mock_resp)

        qrcode_id, scan_url = await chan._fetch_qr_code()
        assert qrcode_id == "qr_id_123"
        assert scan_url == "https://scan.example.com/qr"

    @pytest.mark.asyncio
    async def test_fetch_qr_fallback_to_qrcode_id(self, chan):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "qrcode": "qr_id_456",
        }
        chan._client.get = AsyncMock(return_value=mock_resp)

        qrcode_id, scan_url = await chan._fetch_qr_code()
        assert qrcode_id == "qr_id_456"
        assert scan_url == "qr_id_456"

    @pytest.mark.asyncio
    async def test_fetch_qr_missing_id_raises(self, chan):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"errcode": -1, "errmsg": "error"}
        chan._client.get = AsyncMock(return_value=mock_resp)

        with pytest.raises(RuntimeError, match="QR code"):
            await chan._fetch_qr_code()

    @pytest.mark.asyncio
    async def test_qr_login_confirmed(self, chan, tmp_path):
        state_dir = tmp_path / "wechat_state"
        state_dir.mkdir()

        qr_resp = MagicMock()
        qr_resp.json.return_value = {"qrcode": "qr_id", "qrcode_img_content": "url"}
        poll1 = MagicMock()
        poll1.json.return_value = {"status": "wait"}
        poll2 = MagicMock()
        poll2.json.return_value = {
            "status": "confirmed",
            "bot_token": "new-bot-token",
            "ilink_bot_id": "@im.bot",
            "baseurl": "https://custom.example.com",
        }

        chan._client.get = AsyncMock(side_effect=[qr_resp, poll1, poll2])
        chan._state_dir = state_dir

        result = await chan._qr_login()
        assert result is True
        assert chan._token == "new-bot-token"

        account = json.loads((state_dir / "account.json").read_text())
        assert account["token"] == "new-bot-token"

    @pytest.mark.asyncio
    async def test_qr_login_expired_then_refresh(self, chan, tmp_path):
        state_dir = tmp_path / "wechat_state"
        state_dir.mkdir()
        chan._state_dir = state_dir

        qr1 = MagicMock()
        qr1.json.return_value = {"qrcode": "qr1", "qrcode_img_content": "url1"}
        expired = MagicMock()
        expired.json.return_value = {"status": "expired"}
        qr2 = MagicMock()
        qr2.json.return_value = {"qrcode": "qr2", "qrcode_img_content": "url2"}
        confirmed = MagicMock()
        confirmed.json.return_value = {
            "status": "confirmed",
            "bot_token": "token-2",
            "ilink_bot_id": "@im.bot",
        }

        chan._client.get = AsyncMock(side_effect=[qr1, expired, qr2, confirmed])

        result = await chan._qr_login()
        assert result is True
        assert chan._token == "token-2"


# =========================================================================
# Poll loop
# =========================================================================


class TestPollOnce:
    @pytest.fixture
    def chan(self, mock_orchestrator):
        c = WechatChannel(mock_orchestrator)
        c._token = "test-token"
        mock_client = MagicMock(spec=httpx.AsyncClient)
        c._client = mock_client
        return c

    @pytest.mark.asyncio
    async def test_poll_updates_cursor(self, chan):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "ret": 0,
            "get_updates_buf": "new_cursor",
            "msgs": [],
        }
        chan._client.post = AsyncMock(return_value=mock_resp)
        chan._get_updates_buf = "old_cursor"

        await chan._poll_once()
        assert chan._get_updates_buf == "new_cursor"

    @pytest.mark.asyncio
    async def test_poll_processes_messages(self, chan):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "ret": 0,
            "msgs": [{
                "message_id": "m1",
                "from_user_id": "@u1",
                "context_token": "ctx_1",
                "item_list": [{"type": ITEM_TEXT, "text_item": {"text": "hello"}}],
            }],
        }
        chan._client.post = AsyncMock(return_value=mock_resp)

        processed: list[ChannelMessage] = []
        orig = chan._on_message
        chan._on_message = lambda msg: processed.append(msg) or orig(msg)

        await chan._poll_once()
        assert len(processed) == 1

    @pytest.mark.asyncio
    async def test_poll_skips_duplicate_messages(self, chan):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "ret": 0,
            "msgs": [{
                "message_id": "dup_1",
                "from_user_id": "@u1",
                "item_list": [{"type": ITEM_TEXT, "text_item": {"text": "first"}}],
            }],
        }
        chan._client.post = AsyncMock(return_value=mock_resp)

        await chan._poll_once()
        await chan._poll_once()

        assert len(chan._processed_ids) == 1

    @pytest.mark.asyncio
    async def test_poll_honours_server_timeout(self, chan):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "ret": 0,
            "longpolling_timeout_ms": 60000,
            "msgs": [],
        }
        chan._client.post = AsyncMock(return_value=mock_resp)

        await chan._poll_once()
        assert chan._next_poll_timeout_s == 60

    @pytest.mark.asyncio
    async def test_poll_session_expired(self, chan):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "ret": 0,
            "errcode": ERRCODE_SESSION_EXPIRED,
            "msgs": [],
        }
        chan._client.post = AsyncMock(return_value=mock_resp)

        await chan._poll_once()
        assert chan._session_pause_until > 0


# =========================================================================
# Send text
# =========================================================================


class TestSendText:
    @pytest.fixture
    def chan(self, mock_orchestrator):
        c = WechatChannel(mock_orchestrator)
        c._token = "test-token"
        mock_client = MagicMock(spec=httpx.AsyncClient)
        c._client = mock_client
        return c

    @pytest.mark.asyncio
    async def test_send_text_builds_correct_body(self, chan):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ret": 0, "errcode": 0}
        chan._client.post = AsyncMock(return_value=mock_resp)

        await chan._send_text("@user1", "hello world", "ctx_token_123")

        call_body = chan._client.post.call_args.kwargs["json"]
        assert call_body["msg"]["to_user_id"] == "@user1"
        assert call_body["msg"]["message_type"] == MESSAGE_TYPE_BOT
        assert call_body["msg"]["message_state"] == MESSAGE_STATE_FINISH
        assert call_body["msg"]["context_token"] == "ctx_token_123"
        assert call_body["msg"]["item_list"][0]["type"] == ITEM_TEXT
        assert call_body["msg"]["item_list"][0]["text_item"]["text"] == "hello world"

    @pytest.mark.asyncio
    async def test_send_reply_splits_long_message(self, chan, mock_orchestrator):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ret": 0, "errcode": 0}
        chan._client.post = AsyncMock(return_value=mock_resp)
        chan._context_tokens["@user1"] = "ctx"

        long_text = "x" * (WEIXIN_MAX_MESSAGE_LEN + 100)
        msg = ChannelMessage(
            session_key="wechat:private:@user1",
            text="hi", user_id="@user1", chat_id="@user1",
            chat_type="private", platform="wechat",
        )
        await chan.send_reply(long_text, msg)

        assert chan._client.post.call_count == 2


# =========================================================================
# MessageBus integration
# =========================================================================


class TestMessageBusIntegration:
    @pytest.fixture
    def chan(self, mock_orchestrator):
        c = WechatChannel(mock_orchestrator)
        return c

    @pytest.mark.asyncio
    async def test_on_message_enqueues(self, chan, mock_orchestrator):
        raw = {
            "message_id": "m1",
            "from_user_id": "@u1",
            "context_token": "ctx_1",
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": "hello"}}],
        }
        await chan._on_message(raw)

        inbound = await chan._bus.inbound("wechat:private:@u1").get()
        assert inbound.session_key == "wechat:private:@u1"
        assert inbound.content == "hello"
        assert inbound.source == "wechat"

    @pytest.mark.asyncio
    async def test_on_message_starts_serve_task(self, chan, mock_orchestrator):
        raw = {
            "message_id": "m2",
            "from_user_id": "@u2",
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": "hi"}}],
        }
        await chan._on_message(raw)

        mock_orchestrator.serve.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_message_stores_chat_id(self, chan):
        raw = {
            "message_id": "m3",
            "from_user_id": "@u3",
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": "hi"}}],
        }
        await chan._on_message(raw)
        assert chan._chat_ids["wechat:private:@u3"] == "@u3"

    @pytest.mark.asyncio
    async def test_on_message_caches_context_token(self, chan):
        raw = {
            "message_id": "m4",
            "from_user_id": "@u4",
            "context_token": "ctx_fresh",
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": "hi"}}],
        }
        await chan._on_message(raw)
        assert chan._context_tokens["@u4"] == "ctx_fresh"

    @pytest.mark.asyncio
    async def test_consume_outbound_sends_reply(self, chan):
        from core.message_bus import OutboundMessage

        chan._chat_ids["wechat:private:@u1"] = "@u1"
        chan._context_tokens["@u1"] = "ctx_1"
        chan._token = "tk"
        chan._stop_event = asyncio.Event()

        await chan._bus.outbound("wechat").put(OutboundMessage(
            session_key="wechat:private:@u1",
            correlation_id="wechat:private:@u1",
            msg_type="final",
            data={"content": "hello from bot"},
        ))

        with patch.object(chan, "_send_text", new_callable=AsyncMock) as mock_send:
            consumer = asyncio.create_task(chan._consume_outbound())
            await asyncio.sleep(0.15)
            chan._stop_event.set()
            await consumer
            mock_send.assert_called_once_with("@u1", "hello from bot", "ctx_1")

    @pytest.mark.asyncio
    async def test_consume_outbound_no_chat_id_drops(self, chan):
        from core.message_bus import OutboundMessage

        chan._token = "tk"
        chan._stop_event = asyncio.Event()

        await chan._bus.outbound("wechat").put(OutboundMessage(
            session_key="wechat:private:@unknown",
            correlation_id="cid",
            msg_type="final",
            data={"content": "reply"},
        ))

        with patch.object(chan, "_send_text", new_callable=AsyncMock) as mock_send:
            consumer = asyncio.create_task(chan._consume_outbound())
            await asyncio.sleep(0.15)
            chan._stop_event.set()
            await consumer
            assert mock_send.call_count == 0

    @pytest.mark.asyncio
    async def test_consume_outbound_no_content(self, chan):
        from core.message_bus import OutboundMessage

        chan._chat_ids["wechat:private:@u1"] = "@u1"
        chan._context_tokens["@u1"] = "ctx_1"
        chan._token = "tk"
        chan._stop_event = asyncio.Event()

        await chan._bus.outbound("wechat").put(OutboundMessage(
            session_key="wechat:private:@u1",
            correlation_id="cid",
            msg_type="final",
            data={},
        ))

        with patch.object(chan, "_send_text", new_callable=AsyncMock) as mock_send:
            consumer = asyncio.create_task(chan._consume_outbound())
            await asyncio.sleep(0.15)
            chan._stop_event.set()
            await consumer
            assert mock_send.call_count == 0


# =========================================================================
# Channel lifecycle
# =========================================================================


class TestWechatChannelLifecycle:
    def test_channel_name(self, mock_orchestrator):
        chan = WechatChannel(mock_orchestrator)
        assert chan.channel_name == "wechat"

    @pytest.mark.asyncio
    async def test_shutdown_cleans_up(self, mock_orchestrator):
        chan = WechatChannel(mock_orchestrator)
        chan._stop_event = asyncio.Event()
        chan._consumer_task = asyncio.create_task(asyncio.sleep(0))
        await chan.shutdown()
        assert chan._stop_event.is_set()

    @pytest.mark.asyncio
    async def test_send_reply_sends_text(self, mock_orchestrator, tmp_path):
        chan = WechatChannel(mock_orchestrator)
        chan._token = "tk"
        mock_client = MagicMock(spec=httpx.AsyncClient)
        chan._client = mock_client
        chan._context_tokens["@u1"] = "ctx_1"

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ret": 0, "errcode": 0}
        chan._client.post = AsyncMock(return_value=mock_resp)

        msg = ChannelMessage(
            session_key="wechat:private:@u1",
            text="hi", user_id="@u1", chat_id="@u1",
            chat_type="private", platform="wechat",
        )
        await chan.send_reply("hello back", msg)

        chan._client.post.assert_called_once()
        body = chan._client.post.call_args.kwargs["json"]
        assert body["msg"]["to_user_id"] == "@u1"


# =========================================================================
# _split_text (shared helper)
# =========================================================================


class TestSplitText:
    def test_short_message_single_chunk(self):
        assert _split_text("Hello", 4000) == ["Hello"]

    def test_long_splits(self):
        text = "x" * 5000
        chunks = _split_text(text, 4000)
        assert len(chunks) == 2
        assert len(chunks[0]) == 4000
        assert len(chunks[1]) == 1000

    def test_newline_split_preferred(self):
        text = "a" * 3000 + "\n" + "b" * 2000
        chunks = _split_text(text, 4000)
        assert len(chunks) >= 2
        assert chunks[0].endswith("a") or len(chunks[0]) == 3001


# =========================================================================
# Deduplication
# =========================================================================


class TestDeduplication:
    @pytest.fixture
    def chan(self, mock_orchestrator):
        c = WechatChannel(mock_orchestrator)
        c._token = "tk"
        c._client = MagicMock(spec=httpx.AsyncClient)
        c._processed_ids = OrderedDict()
        return c


@pytest.fixture
def mock_orchestrator():
    orch = MagicMock()
    orch.serve = AsyncMock()
    orch.start_services = AsyncMock()
    orch.scheduled_tasks = MagicMock()
    orch.scheduled_tasks.set_deliver = MagicMock()
    return orch

    @pytest.mark.asyncio
    async def test_dedup_by_message_id(self, chan):
        raw = {
            "message_id": "dup_id",
            "from_user_id": "@u1",
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": "hi"}}],
        }
        await chan._on_message(raw)
        await chan._on_message(raw)
        assert len(chan._processed_ids) == 1

