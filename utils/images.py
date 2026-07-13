"""Image file utilities — convert local images to base64 data URLs for multimodal LLM input.

No third-party dependencies. Uses only stdlib for zero-cost installation.
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUPPORTED_IMAGE_EXTENSIONS: set[str] = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

MAX_IMAGE_BYTES: int = 20_000_000  # 20 MB — most LLM providers cap around here


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def file_to_data_url(path: str) -> str | None:
    """Read an image file and return a base64 data URL.

    Returns ``None`` if the file does not exist, is not a supported image
    format, or exceeds ``MAX_IMAGE_BYTES``.
    """
    p = Path(path)

    if not p.is_file():
        return None

    suffix = p.suffix.lower()
    if suffix not in SUPPORTED_IMAGE_EXTENSIONS:
        return None

    if suffix == ".jpg":
        suffix = ".jpeg"

    file_size = p.stat().st_size
    if file_size > MAX_IMAGE_BYTES:
        return None

    mime_type = mimetypes.guess_type(p.name)[0] or f"image/{suffix[1:]}"

    try:
        data = p.read_bytes()
    except OSError:
        return None

    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime_type};base64,{b64}"
