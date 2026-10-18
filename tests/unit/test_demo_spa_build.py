"""The demo's SPA staleness check (scripts/demo/instance.py)."""

import os
from pathlib import Path

import pytest

from scripts.demo import instance
from scripts.demo.instance import spa_is_stale


def _touch(path: Path, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    os.utime(path, (mtime, mtime))


@pytest.fixture
def webapp(tmp_path: Path) -> Path:
    root = tmp_path / "webapp"
    _touch(root / "src" / "main.tsx", 1_000)
    _touch(root / "src" / "components" / "Chart.tsx", 1_000)
    _touch(root / "public" / "icon.png", 1_000)
    for name in (
        "index.html",
        "package.json",
        "package-lock.json",
        "vite.config.ts",
        "tsconfig.app.json",
    ):
        _touch(root / name, 1_000)
    _touch(root / "dist" / "index.html", 2_000)
    return root


def test_fresh_dist_is_not_stale(webapp: Path) -> None:
    assert not spa_is_stale(webapp)


def test_missing_dist_is_stale(webapp: Path) -> None:
    (webapp / "dist" / "index.html").unlink()
    assert spa_is_stale(webapp)


@pytest.mark.parametrize(
    "changed",
    [
        "src/components/Chart.tsx",
        "public/icon.png",
        "index.html",
        "package.json",
        "package-lock.json",
        "vite.config.ts",
        "tsconfig.app.json",
    ],
)
def test_any_newer_build_input_makes_dist_stale(webapp: Path, changed: str) -> None:
    _touch(webapp / changed, 3_000)
    assert spa_is_stale(webapp)


def test_unrelated_files_do_not_make_dist_stale(webapp: Path) -> None:
    _touch(webapp / "e2e" / "smoke.spec.ts", 3_000)
    _touch(webapp / "node_modules" / "pkg" / "index.js", 3_000)
    assert not spa_is_stale(webapp)


def test_ensure_spa_built_rebuilds_only_when_stale(
    webapp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(instance, "ROOT", webapp.parent)
    monkeypatch.setattr(instance.subprocess, "run", lambda cmd, check: calls.append(cmd))

    instance.ensure_spa_built()
    assert calls == []

    _touch(webapp / "src" / "main.tsx", 3_000)
    instance.ensure_spa_built()
    assert calls == [["npm", "--prefix", str(webapp), "run", "build"]]
