"""Xiaohongshu publish tool — post content via browser automation."""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from loguru import logger

from .guard import Capability
from .tool import Tool, ToolResult

_SCRIPT_NAME = "xhs_publish.py"

NotifyCb = Callable[[dict[str, Any]], Awaitable[None]]


class XiaohongshuPublishTool(Tool):
    """Publish a note to Xiaohongshu via Playwright browser automation.

    Requires a standalone ``scripts/xhs_publish.py`` script and Playwright
    installed (``pip install playwright && playwright install chromium``).

    On first use, run the script manually to authenticate and save cookies:
    ``python scripts/xhs_publish.py --login``
    """

    name = "xiaohongshu_publish"
    description = (
        "Publish a note to Xiaohongshu (RED/小红书). "
        "Use for: posting content to Xiaohongshu, creating RED notes with images. "
        "NOT for: general social media posting, reading Xiaohongshu content, "
        "or platforms other than Xiaohongshu. "
        "Provide a title, cover image content, optional caption, "
        "and optional local image paths. Returns the note ID on success."
    )
    parameters = {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Note title (max 20 characters).",
            },
            "content": {
                "type": "string",
                "description": (
                    "Main content rendered onto the first auto-generated "
                    "image (e.g. turtle-soup puzzle / 汤面). "
                ),
            },
            "content2": {
                "type": "string",
                "description": (
                    "Optional second content rendered onto a second image "
                    "(e.g. turtle-soup answer / 汤底). When provided, "
                    "both images are uploaded as a multi-image post — "
                    "first image shows 汤面, second image shows 汤底."
                ),
            },
            "caption": {
                "type": "string",
                "description": (
                    "Optional text for the note's body box — call-to-action "
                    "lines and hashtags that should NOT appear on the image "
                    "(e.g. '左滑查看答案 #海龟汤 #推理'). "
                    "Falls back to content when omitted."
                ),
            },
            "images": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional list of local image file paths to upload."
                ),
            },
        },
        "required": ["title", "content"],
    }

    capabilities = {Capability.NETWORK}
    _scopes = {"core"}
    _parallel = False

    def __init__(self) -> None:
        self._notify: NotifyCb | None = None

    def set_notify(self, notify: NotifyCb) -> None:
        """Inject a callback invoked with the rendered draft when a publish is
        auto-filled but *not confirmed*.

        Lets a channel push the cover image + caption to the operator for
        manual publishing.  When unset, an unconfirmed publish just returns the
        plain error (no push).
        """
        self._notify = notify

    async def execute(self, **kwargs: Any) -> ToolResult:
        title = str(kwargs.get("title", "")).strip()
        content = str(kwargs.get("content", "")).strip()
        caption = str(kwargs.get("caption", "")).strip()
        content2 = str(kwargs.get("content2", "")).strip()
        images = kwargs.get("images") or []

        if not title:
            return ToolResult(
                success=False, content="", error="title is required"
            )
        if not content:
            return ToolResult(
                success=False, content="", error="content is required"
            )

        script = self._find_script()
        if script is None:
            return ToolResult(
                success=False,
                content="",
                error=(
                    f"Publish script not found: {_SCRIPT_NAME}. "
                    "Place it under scripts/ or install the tool correctly."
                ),
            )

        if not shutil.which("python"):
            return ToolResult(
                success=False,
                content="",
                error="python interpreter not found",
            )

        payload = json.dumps(
            {"title": title, "content": content, "caption": caption,
             "images": images, "content2": content2}
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                "python",
                str(script),
                "--payload",
                payload,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode == 0:
                result = stdout.decode().strip()
                return ToolResult(success=True, content=result)
            else:
                err_msg = (
                    stderr.decode().strip() or stdout.decode().strip()
                )
                fallback = await self._dispatch_fallback(
                    stdout, title=title, content=content, caption=caption
                )
                return ToolResult(
                    success=False,
                    content=fallback,
                    error=f"Publish failed: {err_msg}",
                )
        except FileNotFoundError:
            return ToolResult(
                success=False,
                content="",
                error="python not found — cannot run publish script",
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                content="",
                error=f"Publish error: {exc}",
            )

    async def _dispatch_fallback(
        self, stdout: bytes, *, title: str, content: str, caption: str
    ) -> str:
        """On an *unconfirmed* publish, push the rendered draft to the operator.

        Returns an operator-facing message when a fallback push was dispatched;
        otherwise an empty string, so the caller keeps the plain error and the
        agent does not advance its publish state.
        """
        if self._notify is None:
            return ""
        try:
            data = json.loads(stdout.decode().strip() or "{}")
        except json.JSONDecodeError:
            return ""
        if not isinstance(data, dict) or data.get("status") != "unconfirmed":
            return ""
        draft = {
            "title": title,
            "content": content,
            "caption": caption or data.get("caption", ""),
            "image": data.get("image", ""),
        }
        try:
            await self._notify(draft)
        except Exception:
            logger.opt(exception=True).warning("Xiaohongshu fallback notify failed")
            return ""
        return "自动发布未确认，已把封面图+文案推送到你的微信文件传输助手，请手动发布。"

    def _find_script(self) -> Path | None:
        """Locate the publish script relative to the project root."""
        candidates = [
            Path(__file__).resolve().parent.parent / "scripts" / _SCRIPT_NAME,
            Path.cwd() / "scripts" / _SCRIPT_NAME,
        ]
        for p in candidates:
            if p.exists():
                return p
        return None
