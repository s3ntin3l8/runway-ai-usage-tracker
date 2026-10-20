"""Contract test: the sidecar edge build must trigger on the app/ code it bundles.

The sidecar binary is a PyInstaller bundle of ``scripts/sidecar.py`` (the CLI
spec) and ``sidecar_app/__main__.py`` (the tray specs). Both reach shared
``app/`` modules — most importantly ``app.services.model_normalization``,
which decides the cost bucket every collected event is billed under. A change
to one of those modules changes the sidecar binary, so the rolling edge build
has to watch them.

``sidecar-edge.yml`` did not: its ``paths:`` filter covered only the
sidecar's own trees (``scripts/sidecar_pkg/**``, ``sidecar_app/**``,
``installer/**``). PR #693 canonicalised the Gemini 4 Argon model id in
``app/services/model_normalization.py`` and so reached the server image but
not the sidecar — the fix only reached installed sidecars because an unrelated
``installer/`` change happened to trigger a build in the same window. The
failure is silent: the server and the sidecar disagree about the model id,
and only the sidecar's copy decides what lands in ``usage_events``.

This test computes the same transitive ``app/`` import closure the bundler
walks and asserts that every module in it is covered by the workflow's
``paths:`` filter, so the next shared module to be imported by the sidecar
fails here instead of shipping a stale collector.

The closure is computed the way PyInstaller computes it — from every spec's
entry point, following relative imports, and treating a spec's declared
``hiddenimports`` as roots — because a spec that ships a module the static
scan would miss still bundles it. A partial closure is the one way this test
could pass while missing a module, so the scan fails loudly on anything it
cannot resolve rather than quietly dropping it.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
EDGE_WF = ROOT / ".github" / "workflows" / "sidecar-edge.yml"
SPEC_DIR = ROOT / "sidecar_app" / "spec"

# The spec files, each with its own Analysis() entry point.
SPECS = sorted(SPEC_DIR.glob("*.spec"))

# The app/ modules the shipped sidecar binary bundles. Verified against the
# PyInstaller archive of the released `edge.e4f9ea1` Linux-CLI payload: the
# PYZ holds exactly these app.* entries. The test asserts the computed
# closure still contains all of them, so a scan that silently stops finding
# one fails here instead of quietly narrowing the coverage check.
KNOWN_BUNDLED_APP_MODULES = frozenset(
    {
        "app.core.cache",
        "app.core.config",
        "app.core.console",
        "app.core.encryption",
        "app.core.kimi_device",
        "app.core.login_dirs",
        "app.core.net",
        "app.core.strings",
        "app.core.utils",
        "app.models._datetime",
        "app.models.builder",
        "app.models.db",
        "app.models.schemas",
        "app.services.model_normalization",
    }
)


def _module_file(name: str) -> pathlib.Path | None:
    """The file a dotted module name resolves to, or None if it has no file.

    A name can resolve to a module (``pkg/mod.py``) or to a package
    (``pkg/__init__.py``). Importing a package pulls in its ``__init__.py``,
    so that is the file to read when the name is a package.
    """
    parts = name.split(".")
    direct = ROOT.joinpath(*parts).with_suffix(".py")
    if direct.is_file():
        return direct
    package = ROOT.joinpath(*parts) / "__init__.py"
    if package.is_file():
        return package
    return None


def _package_of(path: pathlib.Path) -> str:
    """The dotted package a module belongs to (``app/core/utils.py`` -> ``app.core``).

    A module's package is its parent directory's dotted path; a package's own
    ``__init__.py`` belongs to the package it initialises.
    """
    rel = path.relative_to(ROOT)
    parts = list(rel.parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts = parts[:-1]
    return ".".join(parts)


def _resolve_relative(importer: pathlib.Path, level: int, module: str | None) -> str:
    """Resolve a relative import to the absolute module name it refers to.

    ``level`` is the number of leading dots: one dot means the importer's own
    package, two dots its parent, and so on. ``from ..models.db import X`` in
    ``app/core/utils.py`` is ``app.models.db``.
    """
    package = _package_of(importer)
    parts = package.split(".") if package else []
    # One dot = the package itself, so a level of n climbs n-1 levels.
    if level > 1:
        parts = parts[: len(parts) - (level - 1)]
    if module:
        parts += module.split(".")
    return ".".join(parts)


def _imports_of(path: pathlib.Path) -> set[str]:
    """Absolute module names imported by ``path``, at any depth.

    Raises on a file it cannot parse: silently treating it as importing
    nothing would truncate the closure, and a truncated closure makes the
    coverage assertion below pass while missing a module.
    """
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - unreadable source file
        raise AssertionError(f"cannot read {path}: {exc}") from exc
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        raise AssertionError(f"cannot parse {path}: {exc}") from exc

    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = _resolve_relative(path, node.level, node.module)
                if base:
                    found.add(base)
                found.update(f"{base}.{alias.name}" for alias in node.names if base)
            elif node.module:
                found.add(node.module)
                found.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def _spec_entry_point(spec: pathlib.Path) -> pathlib.Path | None:
    """The script a spec's Analysis() runs — the bundle's import root.

    The path is an ``os.path.join(_ROOT, "a", "b")`` call, so every quoted
    segment is collected, not just the first.
    """
    text = spec.read_text(encoding="utf-8")
    match = re.search(
        r"Analysis\(\s*\[os\.path\.join\(_ROOT,\s*(?P<segments>.*?)\]\s*,\s*\n",
        text,
        re.DOTALL,
    )
    if not match:
        return None
    segments = re.findall(r'"([^"]+)"', match.group("segments"))
    if not segments:
        return None
    entry = ROOT.joinpath(*segments)
    return entry if entry.is_file() else None


def _spec_hidden_imports(spec: pathlib.Path) -> set[str]:
    """The ``app.*`` modules a spec declares as hiddenimports.

    A spec that names a module explicitly ships it even when the static scan
    would not find it, so these are roots of the closure too.
    """
    return set(re.findall(r'"(app\.[\w.]+)"', spec.read_text(encoding="utf-8")))


def _bundled_app_modules() -> dict[str, pathlib.Path]:
    """The transitive ``app.*`` import closure every sidecar spec bundles.

    Mirrors PyInstaller's static scan: start at each spec's entry point and
    its declared hiddenimports, follow every import (including the ones
    inside function bodies, which is how ``app.core.utils`` reaches
    ``app.models.db``), and collect what lives under ``app/``.
    """
    seen: set[pathlib.Path] = set()
    queue: list[pathlib.Path] = []
    app_modules: dict[str, pathlib.Path] = {}

    for spec in SPECS:
        entry = _spec_entry_point(spec)
        if entry is not None:
            queue.append(entry)
        for name in _spec_hidden_imports(spec):
            target = _module_file(name)
            if target is not None:
                app_modules.setdefault(name, target)
                queue.append(target)

    while queue:
        current = queue.pop()
        if current in seen:
            continue
        seen.add(current)
        for name in _imports_of(current):
            target = _module_file(name)
            if target is None:
                continue
            if name == "app" or name.startswith("app."):
                app_modules.setdefault(name, target)
            queue.append(target)

    # Every package in the closure contributes its own __init__.py to the
    # bundle, whether or not anything imports the package by name: importing
    # app.core.utils executes app/core/__init__.py on the way. Those files
    # carry code (app/__init__.py holds __version__), so a change to one
    # changes the sidecar too and the filter has to watch it.
    for name in list(app_modules):
        parts = name.split(".")
        for depth in range(1, len(parts)):
            parent = ".".join(parts[:depth])
            target = _module_file(parent)
            if target is not None:
                app_modules.setdefault(parent, target)
    return app_modules


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a GitHub path-filter glob into a regex.

    ``**`` matches any number of path segments, ``*`` matches within one
    segment — the same semantics GitHub uses for a workflow's ``paths:``.
    """
    out: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            out.append("(?:.*/)?")
            index += 3
        elif pattern.startswith("**", index):
            out.append(".*")
            index += 2
        elif pattern[index] == "*":
            out.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(pattern[index]))
            index += 1
    return re.compile("^" + "".join(out) + "$")


def _edge_watch_paths() -> list[str]:
    """The ``paths:`` filter of the sidecar edge build's push trigger."""
    workflow = yaml.safe_load(EDGE_WF.read_text(encoding="utf-8"))
    # PyYAML follows YAML 1.1, where a bare ``on:`` key parses as the boolean
    # True. Accept either spelling so the test survives a YAML 1.2 parser.
    trigger = workflow.get("on", workflow.get(True))
    assert isinstance(trigger, dict), "sidecar-edge.yml has no push trigger"
    push = trigger["push"]
    return list(push.get("paths", []))


class TestSidecarEdgeBuildWatchesBundledAppCode:
    def test_edge_build_watches_the_app_modules_the_sidecar_bundles(self):
        watched = [_glob_to_regex(pattern) for pattern in _edge_watch_paths()]
        uncovered = [
            path.relative_to(ROOT).as_posix()
            for path in _bundled_app_modules().values()
            if not any(rx.match(path.relative_to(ROOT).as_posix()) for rx in watched)
        ]
        assert not uncovered, (
            "sidecar-edge.yml's paths: filter does not cover these app/ modules the "
            f"sidecar binary bundles: {uncovered}. A change to them changes the "
            "sidecar but would not rebuild it, so installed sidecars would keep "
            "running the old code. Add the path to the filter."
        )

    def test_the_computed_closure_matches_the_shipped_binary(self):
        # Guards the test above against a scan that stops finding a module:
        # the released sidecar bundles exactly this set, so a closure that
        # shrank means the scan truncated, not that the bundle did.
        assert _bundled_app_modules().keys() >= KNOWN_BUNDLED_APP_MODULES

    def test_model_normalization_is_among_the_bundled_modules(self):
        # The module PR #693 changed. If it ever leaves the bundle the
        # assertion above stops covering the case this test exists for.
        assert "app.services.model_normalization" in _bundled_app_modules()

    def test_every_spec_contributes_to_the_closure(self):
        # The tray specs bundle scripts/sidecar.py as data and hand-declare
        # their app.* imports, so their entry point has to be a root too.
        entries = {spec.name: _spec_entry_point(spec) for spec in SPECS}
        assert all(entry is not None for entry in entries.values()), entries

    @pytest.mark.parametrize("pattern", ["app/core/**", "app/models/**"])
    def test_shared_app_trees_are_watched(self, pattern):
        # The two app/ directories the sidecar imports from wholesale. Listed
        # explicitly so a future refactor that narrows the filter to
        # individual files has to update this test on purpose.
        assert pattern in _edge_watch_paths()
