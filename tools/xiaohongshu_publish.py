"""Xiaohongshu publish tool — post content via browser automation."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

from .guard import Capability
from .tool import Tool, ToolResult

_SCRIPT_NAME = "xhs_publish.py"


class XiaohongshuPublishTool(Tool):
    """Publish a note to Xiaohongshu via Playwright browser automation.

    Requires a standalone ``scripts/xhs_publish.py`` script and Playwright
    installed (``pip install playwright && playwright install chromium``).

    On first use, run the script manually to authenticate and save cookies:
    ``python scripts/xhs_publish.py --login``
    """

    name = "xiaohongshu_publish"
    description = (
        "Publish a note to Xiaohongshu (RED). "
        "Provide a title, the full markdown/plain-text body, "
        "and optionally a list of local image file paths. "
        "Returns the note ID on success."
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
                "description": "Full note body in plain text or markdown.",
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

    async def execute(self, **kwargs: Any) -> ToolResult:
        title = str(kwargs.get("title", "")).strip()
        content = str(kwargs.get("content", "")).strip()
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
            {"title": title, "content": content, "images": images}
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
                return ToolResult(
                    success=False,
                    content="",
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
