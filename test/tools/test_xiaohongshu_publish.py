"""Tests for XiaohongshuPublishTool — payload construction and validation."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from tools.xiaohongshu_publish import XiaohongshuPublishTool


class _FakeProc:
    def __init__(self, returncode: int = 0, stdout: bytes = b"{}", stderr: bytes = b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


def _captured_payload(create_mock: AsyncMock) -> dict:
    """Extract the JSON payload passed after the ``--payload`` flag."""
    args = create_mock.call_args.args
    idx = args.index("--payload")
    return json.loads(args[idx + 1])


class TestXiaohongshuPublishPayload:
    async def test_caption_forwarded_into_payload(self):
        tool = XiaohongshuPublishTool()
        with (
            patch("shutil.which", return_value="/usr/bin/python"),
            patch.object(tool, "_find_script", return_value="scripts/xhs_publish.py"),
            patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=_FakeProc()),
            ) as create_mock,
        ):
            result = await tool.execute(
                title="谜题",
                content="纯谜题正文",
                caption="答案明天揭晓\n#海龟汤 #推理",
            )

        assert result.success
        payload = _captured_payload(create_mock)
        assert payload["content"] == "纯谜题正文"
        assert payload["caption"] == "答案明天揭晓\n#海龟汤 #推理"

    async def test_caption_defaults_to_empty_string(self):
        tool = XiaohongshuPublishTool()
        with (
            patch("shutil.which", return_value="/usr/bin/python"),
            patch.object(tool, "_find_script", return_value="scripts/xhs_publish.py"),
            patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=_FakeProc()),
            ) as create_mock,
        ):
            result = await tool.execute(title="谜题", content="正文")

        assert result.success
        payload = _captured_payload(create_mock)
        assert payload["caption"] == ""

    async def test_missing_content_rejected(self):
        tool = XiaohongshuPublishTool()
        result = await tool.execute(title="谜题", content="", caption="#tag")
        assert not result.success
        assert "content is required" in result.error

    def test_caption_in_schema(self):
        props = XiaohongshuPublishTool.parameters["properties"]
        assert "caption" in props
        assert "caption" not in XiaohongshuPublishTool.parameters["required"]
        assert "content" in XiaohongshuPublishTool.parameters["required"]


class TestXiaohongshuPublishFallback:
    async def test_unconfirmed_triggers_notify(self):
        tool = XiaohongshuPublishTool()
        notify = AsyncMock()
        tool.set_notify(notify)
        stdout = json.dumps({
            "status": "unconfirmed",
            "verified": False,
            "image": "/tmp/cover.png",
            "caption": "答案见图",
        }).encode()
        with (
            patch("shutil.which", return_value="/usr/bin/python"),
            patch.object(tool, "_find_script", return_value="scripts/xhs_publish.py"),
            patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=_FakeProc(
                    returncode=2, stdout=stdout, stderr="发布未确认".encode()
                )),
            ),
        ):
            result = await tool.execute(
                title="海龟汤答案", content="正文", caption="答案见图"
            )

        assert not result.success
        notify.assert_awaited_once()
        draft = notify.await_args.args[0]
        assert draft["image"] == "/tmp/cover.png"
        assert draft["title"] == "海龟汤答案"
        assert "文件传输助手" in result.content

    async def test_unconfirmed_without_notify_returns_plain_error(self):
        tool = XiaohongshuPublishTool()
        stdout = json.dumps({"status": "unconfirmed", "verified": False}).encode()
        with (
            patch("shutil.which", return_value="/usr/bin/python"),
            patch.object(tool, "_find_script", return_value="scripts/xhs_publish.py"),
            patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=_FakeProc(
                    returncode=2, stdout=stdout, stderr=b"unconfirmed"
                )),
            ),
        ):
            result = await tool.execute(title="t", content="c")

        assert not result.success
        assert "Publish failed" in result.error
        assert result.content == ""


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
