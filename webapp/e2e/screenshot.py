#!/usr/bin/env python3
"""Screenshot helper for visual QA of the v2 webapp during development.

Usage: python3 e2e/screenshot.py [route] [outfile-prefix]
Takes desktop (1440), tablet (768) and mobile (390) shots of the given route
against the Vite dev server, injecting the admin key from the environment
(RUNWAY_ADMIN_KEY) into localStorage before load.
"""

import os
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get("RUNWAY_WEB_URL", "http://localhost:5173")
CHROMIUM = os.path.expanduser("~/.cache/ms-playwright/chromium-1155/chrome-linux/chrome")

VIEWPORTS = {
    "desktop": {"width": 1440, "height": 900},
    "tablet": {"width": 768, "height": 1024},
    "mobile": {"width": 390, "height": 844},
}


def main() -> None:
    route = sys.argv[1] if len(sys.argv) > 1 else "/"
    prefix = sys.argv[2] if len(sys.argv) > 2 else "shot"
    admin_key = os.environ.get("RUNWAY_ADMIN_KEY", "")
    out_dir = os.path.join(os.path.dirname(__file__), "shots")
    os.makedirs(out_dir, exist_ok=True)

    with sync_playwright() as p:
        kwargs = {}
        if os.path.exists(CHROMIUM):
            kwargs["executable_path"] = CHROMIUM
        browser = p.chromium.launch(**kwargs)
        scheme = os.environ.get("RUNWAY_COLOR_SCHEME", "dark")
        for name, viewport in VIEWPORTS.items():
            context = browser.new_context(viewport=viewport, color_scheme=scheme)
            if admin_key:
                context.add_init_script(f"localStorage.setItem('runway_admin_key', {admin_key!r});")
            page = context.new_page()
            page.goto(BASE + route, wait_until="networkidle")
            page.wait_for_timeout(1200)
            path = os.path.join(out_dir, f"{prefix}-{name}.png")
            page.screenshot(path=path, full_page=True)
            print(path)
            context.close()
        browser.close()


if __name__ == "__main__":
    main()
