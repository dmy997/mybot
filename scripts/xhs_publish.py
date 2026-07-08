#!/usr/bin/env python3
"""Publish a note to Xiaohongshu (RED) via Playwright browser automation.

Usage:
  python scripts/xhs_publish.py --login
  python scripts/xhs_publish.py --assist --title "T" --content "C" [--caption "CTA #tag"]
  python scripts/xhs_publish.py --payload '{"title":"T","content":"C","caption":"CTA #tag"}'

Cookie file: ``scripts/xhs_cookies.json`` (created after --login).
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

COOKIE_FILE = Path(__file__).resolve().parent / "xhs_cookies.json"
CREATOR_URL = "https://creator.xiaohongshu.com"
PUBLISH_URL = "https://creator.xiaohongshu.com/publish/publish"
NOTE_MANAGER_URL = "https://creator.xiaohongshu.com/new/note-manager"

# The 发布 submit control is a Vue custom element (`<xhs-publish-btn>`) that
# mounts its button inside a *closed* shadow root — unreachable by Playwright
# locators, coordinate clicks, or document.querySelector.  Forcing every shadow
# root open (before any page script runs) exposes the inner button so
# `xhs-publish-btn >> text=发布` resolves it.
_FORCE_OPEN_SHADOW = """
(() => {
  const orig = Element.prototype.attachShadow;
  Element.prototype.attachShadow = function (init) {
    if (init && init.mode === 'closed') init = { ...init, mode: 'open' };
    return orig.call(this, init);
  };
})();
"""


def _text_to_image(title: str, content: str) -> str:
    """Render *title* + *content* to a RED-style card PNG, return its path.

    Xiaohongshu's image-note publish flow only reveals the title/body inputs
    after at least one image is uploaded, so text-only posts need a generated
    cover card.
    """
    try:
        from xhs_text_image import _write  # same-dir sibling script
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from xhs_text_image import _write

    out = Path(tempfile.gettempdir()) / f"xhs_card_{abs(hash(title + content)) % 10**8}.png"
    return _write(out, title, content)


def _check_playwright() -> None:
    try:
        import playwright  # noqa: F401
    except ImportError:
        print(
            "Playwright is not installed. Run:\n"
            "  pip install playwright && playwright install chromium"
        )
        sys.exit(1)


def _load_cookies() -> list[dict]:
    if COOKIE_FILE.exists():
        return json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
    return []


def _save_cookies(cookies: list[dict]) -> None:
    COOKIE_FILE.write_text(
        json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


def login() -> None:
    _check_playwright()
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800}, locale="zh-CN"
        )
        page = context.new_page()
        page.goto(CREATOR_URL, wait_until="domcontentloaded")

        print("请在弹出的浏览器中登录小红书创作中心...")
        print("登录完成后按 Enter 保存 cookie...")
        input()

        cookies = context.cookies()
        _save_cookies(cookies)
        print(f"Cookie 已保存到 {COOKIE_FILE}")

        context.close()
        browser.close()


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------


def _click_creator_tab(page, text: str) -> bool:
    """Click the on-screen creator tab (上传图文 / 上传视频) whose label matches.

    The header renders duplicate tab nodes, some positioned far off-screen
    (x≈-9710); filtering by a non-negative bounding box picks the real one.
    The clickable target is the ``.creator-tab`` container, not the inner
    ``<span>`` (whose pointer events the container intercepts).
    """
    for el in page.locator("div.creator-tab").all():
        try:
            if text in el.inner_text():
                box = el.bounding_box()
                if box and box["x"] >= 0 and box["y"] >= 0:
                    el.click()
                    return True
        except Exception:
            continue
    return False


def _fill_note(page, title: str, content: str, images: list[str], caption: str) -> None:
    """Open the image-note composer and fill title, cover image, and body.

    Stops short of the final submit — the 发布 control is a closed-shadow
    ``<xhs-publish-btn>`` web component whose inner button is not reachable
    from a headless context, so submitting is handled by the caller.
    """
    page.goto(PUBLISH_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(4000)

    # The publish page defaults to the 上传视频 (video) tab; switch to
    # 上传图文 (image/text) so the image uploader and title/body inputs appear.
    _click_creator_tab(page, "上传图文")
    page.wait_for_timeout(2000)

    # Upload images — this reveals the title/body inputs.
    page.locator('input[type="file"]').first.set_input_files(images)
    page.wait_for_timeout(8000)

    # Title
    title_input = page.locator('[placeholder*="标题"]').first
    if not title_input:
        title_input = page.locator(
            '[class*="title"] input, [class*="title"] textarea'
        ).first
    title_input.fill(title[:20])
    page.wait_for_timeout(500)

    # Body — the note editor is a TipTap/ProseMirror contenteditable, not a
    # plain input.  caption carries CTA + hashtags; fall back to content when
    # no caption is supplied.
    body_text = caption or content
    editor = page.locator('div.tiptap, [contenteditable="true"]').first
    editor.click()
    page.wait_for_timeout(300)
    lines = body_text.split("\n")
    for i, line in enumerate(lines):
        if line:
            page.keyboard.insert_text(line)
        if i < len(lines) - 1:
            page.keyboard.press("Enter")
    page.wait_for_timeout(1000)


def _distinctive(title: str) -> str:
    """A stable, human-visible slice of the title for note-manager matching."""
    core = title.strip().lstrip("🐢🕵️🔍✨🎉💡🤔 ").strip()
    core = core.replace(" ", "").replace("·", "").replace("|", "")
    return core[-5:] if len(core) >= 5 else core


def _verify_published(page, title: str) -> bool:
    """Confirm the note actually posted by checking the creator note-manager.

    The composer never assumes success — it verifies against the published /
    审核中 list so a swallowed submit is reported honestly.
    """
    try:
        page.goto(NOTE_MANAGER_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(6000)
        body = page.inner_text("body")
        key = _distinctive(title)
        return bool(key) and key in body
    except Exception:
        return False


def publish(
    title: str,
    content: str,
    images: list[str] | None = None,
    caption: str = "",
    assist: bool = False,
) -> str:
    _check_playwright()
    from playwright.sync_api import sync_playwright

    cookies = _load_cookies()
    if not cookies:
        print(
            "未找到 cookie。请先运行: "
            f"python {__file__} --login"
        )
        sys.exit(1)

    images = images or []
    # No image supplied → render the text onto a cover card so the
    # image-note publish flow exposes the title/body inputs.
    if not images:
        images = [_text_to_image(title, content)]

    with sync_playwright() as p:
        # assist mode shows the browser so the user can click 发布 themselves;
        # the fully-headless path can fill everything but cannot reliably click
        # the closed-shadow submit web component.
        browser = p.chromium.launch(headless=not assist)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900}, locale="zh-CN"
        )
        context.add_init_script(_FORCE_OPEN_SHADOW)
        context.add_cookies(cookies)
        page = context.new_page()

        try:
            _fill_note(page, title, content, images, caption)

            if assist:
                print("\n✅ 标题 / 正文 / 配图已自动填好。", file=sys.stderr)
                print(
                    "👉 请在弹出的浏览器窗口中核对内容，然后点击【发布】按钮。",
                    file=sys.stderr,
                )
                print(
                    "   发布完成后回到本终端按 Enter 继续验证...", file=sys.stderr
                )
                input()
            else:
                # The shadow root is forced open (see _FORCE_OPEN_SHADOW), so the
                # inner 发布 button resolves through the custom element host.
                submit = page.locator("xhs-publish-btn >> text=发布").first
                submit.scroll_into_view_if_needed()
                submit.click()
                page.wait_for_timeout(3000)
                # Dismiss any post-click confirm prompt.
                for label in ("确认发布", "同意并发布", "继续发布"):
                    prompt = page.locator(f"text={label}").first
                    if prompt.count() and prompt.bounding_box():
                        prompt.click()
                        page.wait_for_timeout(2000)
                        break
                page.wait_for_timeout(4000)

            verified = _verify_published(page, title)
            result = json.dumps(
                {
                    "status": "published" if verified else "unconfirmed",
                    "verified": verified,
                    "image": images[0] if images else "",
                    "caption": caption,
                },
                ensure_ascii=False,
            )
            print(result)
            if not verified:
                print(
                    "发布未确认：标题未出现在笔记管理列表中。"
                    "若为全自动模式，请改用 --assist 手动点击发布。",
                    file=sys.stderr,
                )
                sys.exit(2)
            return result

        except Exception as exc:
            print(f"发布失败: {exc}", file=sys.stderr)
            sys.exit(1)

        finally:
            context.close()
            browser.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Xiaohongshu note publisher")
    parser.add_argument("--login", action="store_true")
    parser.add_argument(
        "--assist",
        action="store_true",
        help="Headed browser; user clicks 发布 manually after auto-fill.",
    )
    parser.add_argument("--title", type=str)
    parser.add_argument("--content", type=str)
    parser.add_argument("--caption", type=str, default="")
    parser.add_argument("--images", type=str, nargs="*", default=[])
    parser.add_argument("--payload", type=str)
    args = parser.parse_args()

    if args.login:
        login()
        return

    if args.payload:
        payload = json.loads(args.payload)
        title = payload.get("title", "")
        content = payload.get("content", "")
        caption = payload.get("caption", "")
        images = payload.get("images", [])
    else:
        title = args.title or ""
        content = args.content or ""
        caption = args.caption or ""
        images = args.images or []

    if not title or not content:
        parser.error("--title and --content required (or use --payload)")

    publish(title, content, images, caption, assist=args.assist)


if __name__ == "__main__":
    main()
