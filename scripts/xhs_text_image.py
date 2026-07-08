#!/usr/bin/env python3
"""Render Chinese text onto a Xiaohongshu-optimized image (3:4 ratio, 1080×1440).

Usage:
  python scripts/xhs_text_image.py --title "海龟汤 #42" --content "汤面正文..." --out /tmp/xhs.png
"""

from __future__ import annotations

import argparse
import textwrap
from pathlib import Path

# Available Noto CJK fonts on this system
_NOTO_SANS = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
_NOTO_SERIF = "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc"

# Canvas: 3:4 portrait, common for RED note cards
_W, _H = 1080, 1440

# Colors — warm, RED-style palette
_CLR_BG = (255, 248, 245)  # off-white with warm pink tint
_CLR_ACCENT = (195, 27, 52)  # xiaohongshu red
_CLR_TITLE = (50, 20, 20)  # dark brown
_CLR_BODY = (80, 50, 50)  # softer brown
_CLR_LIGHT = (255, 255, 255)

# Layout constants
_MARGIN_X = 80
_MARGIN_Y = 100
_TITLE_SIZE = 60
_BODY_SIZE = 42
_BODY_LEADING = 12  # extra line spacing


def _card_background(draw):
    """Draw the warm background with a red accent stripe at the top."""
    draw.rectangle([0, 0, _W, _H], fill=_CLR_BG)
    draw.rectangle([0, 0, _W, 8], fill=_CLR_ACCENT)
    draw.rectangle([_W - 12, 0, _W, 40], fill=_CLR_ACCENT)


def _wrap_text(text: str, font_path: str, draw) -> list[str]:
    """Wrap *text* to lines that fit within (_W - 2*_MARGIN_X)."""
    from PIL import ImageFont

    max_w = _W - 2 * _MARGIN_X
    font_obj = ImageFont.truetype(font_path, _BODY_SIZE)
    lines: list[str] = []
    for paragraph in text.split("\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            lines.append("")
            continue
        # Estimate chars-per-line from a representative full-width character
        bbox = draw.textbbox((0, 0), "永", font=font_obj)
        char_w = max(1, bbox[2] - bbox[0])
        chars_per_line = max(1, int(max_w / char_w))
        for line in textwrap.wrap(paragraph, width=chars_per_line):
            lines.append(line)
    return lines


def _write(tmp_path, title: str, content: str) -> str:
    """Render *title* + *content* onto a RED-style card and save to *tmp_path*."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (_W, _H), _CLR_LIGHT)
    draw = ImageDraw.Draw(img)

    _card_background(draw)

    y = _MARGIN_Y

    # Title (centered)
    title_font = ImageFont.truetype(_NOTO_SERIF, _TITLE_SIZE)
    title_bbox = draw.textbbox((0, 0), title, font=title_font)
    draw.text(
        ((_W - title_bbox[2] - title_bbox[0]) // 2, y),
        title,
        font=title_font,
        fill=_CLR_TITLE,
    )
    y += (title_bbox[3] - title_bbox[1]) + 30

    # Separator
    draw.rectangle([_MARGIN_X, y, _W - _MARGIN_X, y + 2], fill=_CLR_ACCENT)
    y += 50

    # Body
    body_font = ImageFont.truetype(_NOTO_SANS, _BODY_SIZE)
    max_y = _H - _MARGIN_Y

    for line in _wrap_text(content, _NOTO_SANS, draw):
        if y + _BODY_SIZE > max_y:
            break
        bbox = draw.textbbox((0, 0), line or " ", font=body_font)
        draw.text((_MARGIN_X, y), line, font=body_font, fill=_CLR_BODY)
        y += (bbox[3] - bbox[1]) + _BODY_LEADING

    out = Path(tmp_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, format="PNG")
    return str(out)


def main() -> None:
    parser = argparse.ArgumentParser(description="Render text to RED-style image")
    parser.add_argument("--title", required=True)
    parser.add_argument("--content", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    path = _write(args.out, args.title, args.content)
    print(path)


if __name__ == "__main__":
    main()
