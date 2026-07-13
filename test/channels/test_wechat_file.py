"""Tests for WeChat file upload/download support."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from channels.wechat import ITEM_FILE, WechatChannel


class TestSanitiseFilename:
    def test_normal(self):
        assert WechatChannel._sanitise_filename("hello.txt") == "hello.txt"

    def test_path_separator(self):
        assert WechatChannel._sanitise_filename("a/b.txt") == "a_b.txt"
        assert WechatChannel._sanitise_filename("c\\d.txt") == "c_d.txt"

    def test_null_byte(self):
        assert WechatChannel._sanitise_filename("x\0y.txt") == "xy.txt"


class TestParseFileItem:
    """_parse() extracts ITEM_FILE items from raw iLink messages."""

    def test_single_file_item_produces_files_list(self):
        raw = {
            "from_user_id": "user1",
            "message_type": 1,
            "item_list": [
                {
                    "type": ITEM_FILE,
                    "file_item": {
                        "file_name": "report.pdf",
                        "file_url": "https://cdn.example.com/report.pdf",
                        "file_size": 1024,
                    },
                },
            ],
        }
        msg = WechatChannel._parse(raw, allowed_from=None)
        assert msg is not None
        assert len(msg.files) == 1
        assert msg.files[0]["name"] == "report.pdf"
        assert msg.files[0]["url"] == "https://cdn.example.com/report.pdf"
        assert "report.pdf" in msg.text

    def test_file_with_text_combined(self):
        raw = {
            "from_user_id": "user1",
            "message_type": 1,
            "item_list": [
                {"type": 1, "text_item": {"text": "请帮我分析这个文件"}},
                {
                    "type": ITEM_FILE,
                    "file_item": {
                        "file_name": "data.csv",
                        "file_url": "https://cdn.example.com/data.csv",
                        "file_size": 512,
                    },
                },
            ],
        }
        msg = WechatChannel._parse(raw, allowed_from=None)
        assert msg is not None
        assert len(msg.files) == 1
        assert "请帮我分析这个文件" in msg.text
        assert "data.csv" in msg.text

    def test_file_item_missing_name_skipped(self):
        raw = {
            "from_user_id": "user1",
            "message_type": 1,
            "item_list": [
                {
                    "type": ITEM_FILE,
                    "file_item": {"file_url": "https://cdn.example.com/f.txt"},
                },
            ],
        }
        msg = WechatChannel._parse(raw, allowed_from=None)
        assert msg is None  # no content and no valid files

    def test_file_item_missing_url_skipped(self):
        raw = {
            "from_user_id": "user1",
            "message_type": 1,
            "item_list": [
                {"type": ITEM_FILE, "file_item": {"file_name": "f.txt"}},
                {"type": 1, "text_item": {"text": "hello"}},
            ],
        }
        msg = WechatChannel._parse(raw, allowed_from=None)
        assert msg is not None
        assert len(msg.files) == 0


def _mock_bot():
    bot = MagicMock()
    bot._client = MagicMock()
    bot._state_dir = Path(tempfile.mkdtemp())
    # Preserve real static methods
    bot._sanitise_filename = WechatChannel._sanitise_filename
    return bot


class TestDownloadFile:
    @pytest.mark.asyncio
    async def test_download_saves_to_inbox(self):
        bot = _mock_bot()

        resp = MagicMock()
        resp.content = b"hello world"
        resp.raise_for_status = MagicMock()
        bot._client.get = AsyncMock(return_value=resp)

        result = await WechatChannel._download_file(
            bot, "https://cdn.example.com/f.txt", "user1", "test.txt",
        )

        assert result is not None
        saved = Path(result)
        assert saved.exists()
        assert saved.read_bytes() == b"hello world"
        assert "inbox" in str(saved)
        assert "user1" in str(saved)

    @pytest.mark.asyncio
    async def test_download_deduplicate_filename(self):
        bot = _mock_bot()

        inbox = bot._state_dir / "inbox" / "user1"
        inbox.mkdir(parents=True)
        (inbox / "test.txt").write_text("existing")

        resp = MagicMock()
        resp.content = b"new content"
        resp.raise_for_status = MagicMock()
        bot._client.get = AsyncMock(return_value=resp)

        result = await WechatChannel._download_file(
            bot, "https://cdn.example.com/f.txt", "user1", "test.txt",
        )

        assert result is not None
        saved = Path(result)
        assert "test_1.txt" in str(saved) or saved.stem == "test_1"

    @pytest.mark.asyncio
    async def test_download_http_error_returns_none(self):
        bot = _mock_bot()
        bot._client.get = AsyncMock(side_effect=Exception("503"))

        result = await WechatChannel._download_file(
            bot, "https://cdn.example.com/f.txt", "user1", "test.txt",
        )
        assert result is None


class TestUploadFile:
    @pytest.mark.asyncio
    async def test_upload_returns_url(self):
        bot = MagicMock()
        bot._token = "fake-token"
        bot._client = MagicMock()

        resp = MagicMock()
        resp.json = MagicMock(return_value={"media_url": "https://cdn.example.com/uploaded/f.txt"})
        resp.raise_for_status = MagicMock()
        bot._client.post = AsyncMock(return_value=resp)

        with tempfile.NamedTemporaryFile(mode="wb", suffix=".txt", delete=False) as f:
            f.write(b"test content")
            tmp_path = f.name

        try:
            with patch("channels.wechat._make_headers", return_value={}):
                result = await WechatChannel._upload_file(bot, tmp_path)
            assert result == "https://cdn.example.com/uploaded/f.txt"
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_upload_nonexistent_file_returns_none(self):
        bot = MagicMock()
        bot._client = MagicMock()

        result = await WechatChannel._upload_file(bot, "/nonexistent/file.txt")
        assert result is None

    @pytest.mark.asyncio
    async def test_upload_no_url_in_response_returns_none(self):
        bot = MagicMock()
        bot._token = "fake-token"
        bot._client = MagicMock()

        resp = MagicMock()
        resp.json = MagicMock(return_value={"error": "unknown"})
        resp.raise_for_status = MagicMock()
        bot._client.post = AsyncMock(return_value=resp)

        with tempfile.NamedTemporaryFile(mode="wb", suffix=".txt", delete=False) as f:
            f.write(b"test")
            tmp_path = f.name

        try:
            with patch("channels.wechat._make_headers", return_value={}):
                result = await WechatChannel._upload_file(bot, tmp_path)
            assert result is None
        finally:
            Path(tmp_path).unlink(missing_ok=True)
