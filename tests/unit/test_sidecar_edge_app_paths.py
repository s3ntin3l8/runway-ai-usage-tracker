"""Contract test: the sidecar edge build must trigger on the app/ code it bundles.

The sidecar binary is a PyInstaller bundle of ``scripts/sidecar.py`` (the CLI
spec) and ``sidecar_app/__main__.py`` (the tray specs). Both import shared
``app/`` modules — most importantly ``app.services.model_normalization``, which
decides the cost bucket every collected event is billed under. A change to one
of those modules therefore changes the sidecar binary, so the rolling edge
build has to watch them.

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
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
EDGE_WF = ROOT / ".github" / "workflows" / "sidecar-edge.yml"

# The spec files' entry points. The CLI spec bundles scripts/sidecar.py; the
# tray specs bundle sidecar_app/__main__.py (and scripts/sidecar.py as data).
_SPEC_DIR = ROOT / "sidecar_app" / "spec"


def _module_file(name: str) -> pathlib.Path | None:
    """The file a dotted module name resolves to, or None if it has no file."""
    direct = ROOT.joinpath(*name.split(".")).with_suffix(".py")
    if direct.exists():
        return direct
    package = ROOT.joinpath(*name.split(".")) / "__init__.py"
    return package if package.exists() else None


def _imports_of(path: pathlib.Path) -> set[str]:
    """Absolute module names imported by ``path`` (module level and inside bodies)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, SyntaxError):
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                continue
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def _sidecar_entry_points() -> list[pathlib.Path]:
    """The Python files the sidecar bundles as its program entry point."""
    entries = [ROOT / "scripts" / "sidecar.py", ROOT / "sidecar_app" / "__main__.py"]
    return [p for p in entries if p.exists()]


def _bundled_app_modules() -> dict[str, pathlib.Path]:
    """The transitive ``app.*`` import closure the sidecar bundle walks.

    Mirrors PyInstaller's static scan: start at the entry points, follow every
    import (including the ones inside function bodies, which is how
    ``app.core.utils`` reaches ``app.models.db``), and collect what lives
    under ``app/``.
    """
    seen: set[pathlib.Path] = set()
    queue: list[pathlib.Path] = _sidecar_entry_points()
    app_modules: dict[str, pathlib.Path] = {}
    while queue:
        current = queue.pop()
        if current in seen or not current.exists():
            continue
        seen.add(current)
        for name in _imports_of(current):
            target = _module_file(name)
            if target is None:
                continue
            if name == "app" or name.startswith("app."):
                app_modules.setdefault(name, target)
            queue.append(target)
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

    def test_the_bundled_app_closure_is_not_empty(self):
        # Guards the test above: a broken import scan would report an empty
        # closure and the coverage assertion would pass vacuously.
        assert _bundled_app_modules(), "no app/ modules found in the sidecar import closure"

    def test_model_normalization_is_among_the_bundled_modules(self):
        # The module PR #693 changed. If it ever leaves the bundle the
        # assertion above stops covering the case this test exists for.
        assert "app.services.model_normalization" in _bundled_app_modules()

    @pytest.mark.parametrize("pattern", ["app/core/**", "app/models/**"])
    def test_shared_app_trees_are_watched(self, pattern):
        # The two app/ directories the sidecar imports from wholesale. Listed
        # explicitly so a future refactor that narrows the filter to
        # individual files has to update this test on purpose.
        assert pattern in _edge_watch_paths()
