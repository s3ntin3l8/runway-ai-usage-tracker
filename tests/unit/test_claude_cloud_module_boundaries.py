"""The Claude cloud cluster may not reach into another module's private members.

The cluster's shared pieces live in the public support module. A cross-module
read of an underscore-prefixed member is the coupling this refactor removed, so
this guard fails if one reappears.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CLUSTER = sorted(
    [
        *ROOT.glob("app/services/claude_cloud_*.py"),
        ROOT / "app/services/claude_chat_fixture.py",
        *ROOT.glob("app/api/endpoints/claude_cloud_*.py"),
    ]
)


def bound_names(tree: ast.Module) -> set[str]:
    """The names this module binds itself, so a private read of them is local."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.alias):
            names.add(node.asname or node.name.split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
    return names


def private_imports(tree: ast.Module) -> list[str]:
    """A private name imported from another module of this app."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if not node.module.startswith("app."):
            continue
        for alias in node.names:
            if alias.name.startswith("_"):
                found.append(f"{node.module}.{alias.name}")
    return found


def is_module(module: str, name: str) -> bool:
    """True when ``from module import name`` binds a module, not an attribute."""
    base = ROOT.joinpath(*module.split("."))
    return (base / f"{name}.py").is_file() or (base / name / "__init__.py").is_file()


def module_handles(tree: ast.Module) -> set[str]:
    """The aliases that name another module, so their privates are another module's."""
    handles: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            handles.update(alias.asname for alias in node.names if alias.asname)
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if alias.asname and is_module(node.module, alias.name):
                    handles.add(alias.asname)
                elif not alias.asname and is_module(node.module, alias.name):
                    handles.add(alias.name)
    return handles


def private_reads(tree: ast.Module) -> list[str]:
    """A private member read on another module: an unbound name or a module alias."""
    local = bound_names(tree)
    handles = module_handles(tree)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or not node.attr.startswith("_"):
            continue
        base = node.value
        if isinstance(base, ast.Name) and (base.id not in local or base.id in handles):
            found.append(f"{base.id}.{node.attr}")
    return found


@pytest.mark.parametrize("path", CLUSTER, ids=lambda path: path.name)
def test_the_cluster_reads_no_private_member_of_another_module(path: Path):
    tree = ast.parse(path.read_text())
    imported = private_imports(tree)
    assert not imported, f"{path.relative_to(ROOT)} imports the private {imported}"
    read = private_reads(tree)
    assert not read, f"{path.relative_to(ROOT)} reads the private {read}"


def test_the_cluster_is_not_empty():
    assert len(CLUSTER) >= 5
