#!/usr/bin/env python3
"""Publish a note to Xiaohongshu (RED) via Playwright browser automation.

Usage:
  python scripts/xhs_publish.py --login
  python scripts/xhs_publish.py --title "T" --content "C" [--images a.jpg]
  python scripts/xhs_publish.py --payload '{"title":"T","content":"C"}'

Cookie file: ``scripts/xhs_cookies.json`` (created after --login).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

COOKIE_FILE = Path(__file__).resolve().parent / "xhs_cookies.json"
CREATOR_URL = "https://creator.xiaohongshu.com"
PUBLISH_URL = "https://creator.xiaohongshu.com/publish/publish"


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


def publish(title: str, content: str, images: list[str] | None = None) -> str:
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

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800}, locale="zh-CN"
        )
        context.add_cookies(cookies)
        page = context.new_page()

        try:
            page.goto(PUBLISH_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)

            # Upload images
            if images:
                file_input = page.locator('input[type="file"]').first
                if file_input:
                    file_input.set_input_files(images)
                    page.wait_for_timeout(5000)

            # Title
            title_input = page.locator('[placeholder*="标题"]').first
            if not title_input:
                title_input = page.locator(
                    '[class*="title"] input, [class*="title"] textarea'
                ).first
            title_input.fill(title[:20])
            page.wait_for_timeout(500)

            # Body
            body = page.locator('[placeholder*="正文"]').first
            if not body:
                body = page.locator(
                    '[class*="content"] [contenteditable], '
                    '[class*="editor"] [contenteditable]'
                ).first
            if body:
                body.click()
                body.fill(content)
            else:
                editable = page.locator('[contenteditable="true"]').first
                editable.click()
                editable.fill(content)
            page.wait_for_timeout(1000)

            # Publish button
            btn = page.locator(
                'button:has-text("发布"), '
                'button:has-text("发布笔记"), '
                '[class*="publish"] button'
            ).first
            btn.click()
            page.wait_for_timeout(5000)

            current_url = page.url
            note_id = ""
            if "/note/" in current_url:
                note_id = current_url.rstrip("/").rsplit("/", 1)[-1]

            result = json.dumps(
                {"status": "published", "note_id": note_id, "url": current_url},
                ensure_ascii=False,
            )
            print(result)
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
    parser.add_argument("--title", type=str)
    parser.add_argument("--content", type=str)
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
        images = payload.get("images", [])
    else:
        title = args.title or ""
        content = args.content or ""
        images = args.images or []

    if not title or not content:
        parser.error("--title and --content required (or use --payload)")

    publish(title, content, images)


if __name__ == "__main__":
    main()
