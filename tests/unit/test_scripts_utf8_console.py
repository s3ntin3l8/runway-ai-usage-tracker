"""Every CLI that can print a non-ASCII character must guard its console.

Windows gives a redirected stdout/stderr the locale code page (cp1252 by
default). A script that prints a character outside it — an arrow in a
docstring, an emoji in a log line — dies with UnicodeEncodeError as soon as
its output is piped or captured. The repo's own tooling hides this by
running with PYTHONUTF8=1 (see scripts/dev_checks.py), so a user running the
documented command directly hits it.

This module scans the scripts and fails when one can print such a character
without calling ``force_utf8_console()``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def _unencodable(text: str) -> set[str]:
    """Characters the Windows ANSI code page cannot represent."""
    out: set[str] = set()
    for char in text:
        if ord(char) < 128:
            continue
        try:
            char.encode("cp1252")
        except UnicodeEncodeError:
            out.add(char)
    return out


def _script_strings(tree: ast.Module) -> set[str]:
    """Every string literal in the module, the docstring included."""
    strings = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            strings.add(node.value)
    return strings


def _calls_force_utf8_console(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "force_utf8_console":
                return True
    return False


def _scripts() -> list[Path]:
    return sorted(SCRIPTS.glob("*.py"))


def test_the_scan_finds_scripts_to_check():
    # The scan must actually see the scripts — otherwise it passes vacuously.
    assert len(_scripts()) > 20


@pytest.mark.parametrize("script", _scripts(), ids=lambda p: p.name)
def test_a_script_that_can_print_non_ascii_guards_its_console(script: Path):
    tree = ast.parse(script.read_text(encoding="utf-8"), filename=str(script))
    risky = set()
    for text in _script_strings(tree):
        risky |= _unencodable(text)
    if not risky:
        pytest.skip("script cannot print a character cp1252 cannot encode")
    assert _calls_force_utf8_console(tree), (
        f"{script.name} can print {sorted(hex(ord(c)) for c in risky)}, which the "
        "Windows ANSI code page cannot encode — call force_utf8_console() first"
    )


@pytest.mark.parametrize(
    "spec",
    sorted((ROOT / "sidecar_app" / "spec").glob("*.spec")),
    ids=lambda p: p.name,
)
def test_the_frozen_build_declares_the_console_helper(spec: Path):
    # scripts/sidecar.py is bundled as a data file, so PyInstaller never scans
    # its imports — a new one has to be declared by hand.
    assert '"app.core.console"' in spec.read_text(encoding="utf-8")
