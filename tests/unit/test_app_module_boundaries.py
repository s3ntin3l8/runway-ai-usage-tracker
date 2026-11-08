"""No module under ``app/`` may reach into another module's private members.

A cross-module read of an underscore-prefixed member makes the coupling invisible
at the import statement: a refactor on one side silently changes the other side.
Members another module needs are published under a public name, and a module that
other packages import has a public name too. The Claude cloud cluster has its own
narrower guard (``test_claude_cloud_module_boundaries.py``).
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULES = sorted(ROOT.glob("app/**/*.py"))
SELF_NAMES = {"self", "cls", "mcs"}


def dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def private_imports(tree: ast.Module) -> list[str]:
    """A private name, or a private module, imported from another module of this app."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "app" and any(p.startswith("_") for p in parts[1:]):
                    found.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app"):
            parts = node.module.split(".")
            if any(p.startswith("_") and not dunder(p) for p in parts[1:]):
                found.append(node.module)
            found.extend(
                f"{node.module}.{a.name}"
                for a in node.names
                if a.name.startswith("_") and not dunder(a.name)
            )
    return found


def foreign_private_reads(tree: ast.Module) -> list[str]:
    """``something._member`` where ``something`` is a plain name other than self/cls.

    A class reading its own privates through its own name, a sibling instance of the
    same class (``other._entry``) and third-party API (``row._mapping``,
    ``parts._replace``) are allowlisted by ``ALLOWED`` below.
    """
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        if not node.attr.startswith("_") or dunder(node.attr):
            continue
        base = node.value
        if isinstance(base, ast.Name) and base.id not in SELF_NAMES:
            found.append(f"{base.id}.{node.attr}")
    return found


# (file, "name.attr"): reads that are not another app module's private member.
ALLOWED = {
    ("app/core/log_redaction.py", "parts._replace"),  # namedtuple API
    ("app/services/queries/chart_bars.py", "row._mapping"),  # SQLAlchemy Row API
    ("app/services/token_cache_sources.py", "other._entry"),  # same class
}


def declaring_classes(tree: ast.Module) -> set[str]:
    return {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}


@pytest.mark.parametrize("path", MODULES, ids=lambda p: str(p.relative_to(ROOT)))
def test_module_reads_no_private_member_of_another_module(path: Path):
    rel = str(path.relative_to(ROOT))
    tree = ast.parse(path.read_text())
    imported = private_imports(tree)
    assert not imported, f"{rel} imports the private {imported}"
    own = declaring_classes(tree)
    read = [
        r
        for r in foreign_private_reads(tree)
        if (rel, r) not in ALLOWED and r.split(".")[0] not in own
    ]
    assert not read, f"{rel} reads the private {read}"


def test_guard_covers_the_app():
    assert len(MODULES) > 100
