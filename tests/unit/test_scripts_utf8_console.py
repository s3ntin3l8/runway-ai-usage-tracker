"""Every CLI that can print a non-ASCII character must guard its console.

Windows gives a *redirected* stdout/stderr the locale's ANSI code page
(cp1252 on a Western install, cp1251 on a Russian one, cp932 on a Japanese
one). A script that prints a character outside it — an em dash in a
docstring, an arrow in a help text — dies with UnicodeEncodeError as soon
as its output is piped or captured. The repo's own tooling hides this by
running with PYTHONUTF8=1 (see scripts/dev_checks.py), so a user running
the documented command directly hits it.

Only ASCII is safe on every code page, so anything above U+007F counts as
risky here.

This module scans the scripts and fails when one can print such a
character without calling ``force_utf8_console()``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def _non_ascii(text: str) -> set[str]:
    """Characters outside ASCII — not encodable on every Windows code page."""
    return {char for char in text if ord(char) > 127}


def _script_strings(tree: ast.Module) -> set[str]:
    """Every string literal in the module, the docstring included."""
    strings = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            strings.add(node.value)
    return strings


def _calls_force_utf8_console(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            # Matches both `force_utf8_console()` and `console.force_utf8_console()`.
            if ast.unparse(node.func).endswith("force_utf8_console"):
                return True
    return False


def _scripts() -> list[Path]:
    return sorted(SCRIPTS.glob("*.py"))


def _risky_scripts() -> set[str]:
    """Scripts whose output can carry a character no Windows code page has."""
    risky = set()
    for script in _scripts():
        tree = ast.parse(script.read_text(encoding="utf-8"), filename=str(script))
        text = "".join(_script_strings(tree))
        if _non_ascii(text):
            risky.add(script.name)
    return risky


def test_the_scan_detects_the_scripts_it_claims_to():
    # Pin the signal: a blind detector (one that finds nothing) must fail
    # here rather than turn every scan test into a skip.
    risky = _risky_scripts()
    assert "merge_antigravity_accounts.py" in risky
    assert "sidecar.py" in risky
    assert len(risky) > 10


@pytest.mark.parametrize("script", _scripts(), ids=lambda p: p.name)
def test_a_script_that_can_print_non_ascii_guards_its_console(script: Path):
    tree = ast.parse(script.read_text(encoding="utf-8"), filename=str(script))
    risky = set()
    for text in _script_strings(tree):
        risky |= _non_ascii(text)
    if not risky:
        pytest.skip("script cannot print a character outside ASCII")
    assert _calls_force_utf8_console(tree), (
        f"{script.name} can print {sorted(hex(ord(c)) for c in risky)}, which not "
        "every Windows code page can encode — call force_utf8_console() first"
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
