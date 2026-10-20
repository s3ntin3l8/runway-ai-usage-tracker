"""Find NSIS before contacting Chocolatey; bound transient feed failures."""

import os
import shutil
import subprocess
import time
from pathlib import Path


def _compiler() -> Path | None:
    located = shutil.which("makensis")
    if located:
        return Path(located)
    root = os.environ.get("ProgramFiles(x86)")
    candidate = Path(root) / "NSIS" / "makensis.exe" if root else None
    return candidate if candidate and candidate.is_file() else None


def prepare() -> Path:
    """Return an installed compiler, or fail before invoking packaging."""
    compiler = _compiler()
    if compiler:
        return compiler
    for attempt in range(3):
        try:
            subprocess.run(
                ["choco", "install", "nsis", "-y", "--no-progress"], timeout=180, check=False
            )
        except FileNotFoundError as exc:
            raise RuntimeError("Chocolatey is unavailable and NSIS is not installed") from exc
        compiler = _compiler()
        if compiler:
            return compiler
        if attempt < 2:
            print("::warning::NSIS installation unavailable; retrying in five seconds.")
            time.sleep(5)
    raise RuntimeError("NSIS installation failed after three attempts; packaging stopped")


def main() -> None:
    compiler = prepare()
    print(f"NSIS compiler: {compiler}")
    if github_path := os.environ.get("GITHUB_PATH"):
        with Path(github_path).open("a", encoding="utf-8") as output:
            output.write(f"{compiler.parent}\n")


if __name__ == "__main__":
    main()
