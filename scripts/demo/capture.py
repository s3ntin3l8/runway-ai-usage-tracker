"""Reproducible screenshots from an isolated demo instance.

    make screenshots                    # README (dark + light) and PWA manifest set
    make screenshots-landing OUT=<dir>  # landing-page stills + a short loop

Boots scripts/demo/instance.py, runs the Playwright driver in
webapp/scripts/screenshots.mjs against it, and tears the instance down.
"""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

from scripts.demo.env import assert_demo_env, demo_env
from scripts.demo.instance import ROOT, start_demo


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", choices=("readme", "landing"), default="readme")
    parser.add_argument("--out", help="output directory (landing profile)")
    parser.add_argument("--keep", action="store_true", help="keep the scratch dir")
    args = parser.parse_args()
    if args.profile == "landing" and not args.out:
        parser.error("--profile landing needs --out <dir>")

    # Resolve the browser cache before HOME is redirected into the scratch dir.
    browsers = os.environ.get(
        "PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / ".cache" / "ms-playwright")
    )
    demo = start_demo(keep=args.keep, quiet=True)
    try:
        extra = {
            "SCREENSHOTS_PROFILE": args.profile,
            "SCREENSHOTS_WEB_URL": demo.url,
            "PLAYWRIGHT_BROWSERS_PATH": browsers,
        }
        if args.out:
            extra["SCREENSHOTS_OUT_DIR"] = str(Path(args.out).resolve())
        env = demo_env(demo.demo_dir, extra)
        assert_demo_env(env, demo.demo_dir, cwd=ROOT / "webapp")
        subprocess.run(
            ["node", "scripts/screenshots.mjs"], cwd=ROOT / "webapp", env=env, check=True
        )
    finally:
        print("[screenshots] tearing down the demo...")
        demo.stop()


if __name__ == "__main__":
    main()
