"""No module under ``app/`` may reach into another module's private members.

A cross-module read of an underscore-prefixed member makes the coupling invisible
at the import statement: a refactor on one side silently changes the other side.
Members another module needs are published under a public name, and a module that
other packages import has a public name too. The Claude cloud cluster has its own
narrower guard (``test_claude_cloud_module_boundaries.py``).

Ruff's ``TID252`` only bans relative imports from *parent* modules, so
``from . import _x`` passes lint; ``private_imports`` is the only thing that
catches it and must not be dropped as redundant with the lint rule.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULES = sorted(ROOT.glob("app/**/*.py"))
SELF_NAMES = {"self", "cls", "mcs"}


def dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def resolve_import(node: ast.ImportFrom, package: list[str]) -> str | None:
    """The absolute dotted module of ``node``, resolving a relative import against ``package``."""
    if not node.level:
        return node.module
    if node.level > len(package):
        return None
    base = package[: len(package) - node.level + 1]
    return ".".join([*base, *([node.module] if node.module else [])])


def private_imports(tree: ast.Module, package: list[str] | None = None) -> list[str]:
    """A private name, or a private module, imported from another module of this app."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            module = resolve_import(node, package or ["app"])
            if module and module.startswith("app"):
                parts = module.split(".")
                if any(p.startswith("_") and not dunder(p) for p in parts[1:]):
                    found.append(module)
                found.extend(
                    f"{module}.{a.name}"
                    for a in node.names
                    if a.name.startswith("_") and not dunder(a.name)
                )
            continue
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
        elif isinstance(base, ast.Attribute):
            # ``module.obj._private``; ``self.x._private`` is the owner reaching into its own part
            root = base
            while isinstance(root, ast.Attribute):
                root = root.value
            if not (isinstance(root, ast.Name) and root.id in SELF_NAMES):
                found.append(f"...{base.attr}.{node.attr}")
    return found


def string_private_reads(tree: ast.Module) -> list[str]:
    """``getattr(x, "_private")`` / ``setattr`` / ``hasattr`` with a literal private name."""
    found = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"getattr", "setattr", "hasattr", "delattr"}
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
            and node.args[1].value.startswith("_")
            and not dunder(node.args[1].value)
            and not (isinstance(node.args[0], ast.Name) and node.args[0].id in SELF_NAMES)
        ):
            found.append(f"{node.func.id}(..., {node.args[1].value!r})")
    return found


# (file, "name.attr" or "getattr(..., '_name')"): reads that are not another app module's private member.
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
    imported = private_imports(tree, list(path.relative_to(ROOT).with_suffix("").parts[:-1]))
    assert not imported, f"{rel} imports the private {imported}"
    dynamic = [d for d in string_private_reads(tree) if (rel, d) not in ALLOWED]
    assert not dynamic, f"{rel} reads the private {dynamic}"
    own = declaring_classes(tree)
    read = [
        r
        for r in foreign_private_reads(tree)
        if (rel, r) not in ALLOWED and r.split(".")[0] not in own
    ]
    assert not read, f"{rel} reads the private {read}"


def test_guard_covers_every_subpackage():
    covered = {p.relative_to(ROOT).parts[1] for p in MODULES if len(p.relative_to(ROOT).parts) > 2}
    on_disk = {d.name for d in (ROOT / "app").iterdir() if d.is_dir() and d.name != "__pycache__"}
    assert on_disk <= covered


def test_the_guard_catches_what_it_guards():
    def parse(src: str) -> ast.Module:
        return ast.parse(src)

    assert private_imports(parse("from . import _x"), ["app", "services"])
    assert private_imports(parse("from ._mod import y"), ["app", "services"])
    assert private_imports(parse("from .._mod import y"), ["app", "services"])
    assert not private_imports(parse("from .mod import y"), ["app", "services"])
    assert foreign_private_reads(parse("mod.obj._hidden"))
    assert not foreign_private_reads(parse("self.part._hidden"))
    assert string_private_reads(parse("getattr(mod, '_hidden')"))
