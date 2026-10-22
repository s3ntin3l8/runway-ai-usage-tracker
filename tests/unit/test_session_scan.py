"""The suite's mocked ``Session`` must not outlive the test that captured it.

``tests/conftest.py`` patches ``sqlmodel.Session`` with a mock for every
test. A module that reads ``sqlmodel.Session`` at runtime captures that mock,
and it would then outlive the test -- so the fixture walks the modules this
repository owns and restores the real ``Session`` in any of them that still
holds the mock.

The walk is the expensive part, so it is narrowed to the modules that can
capture it. This file pins that narrowing: a top-level package the
repository owns and the scan does not know about would be skipped silently.

The guard and the scan share one predicate -- ``is_repository_module`` -- so
they cannot drift apart again. A predicate that matched the submodules but
not the package roots would leave ``sys.modules["app"]`` unrepaired, and a
guard derived from the prefix list rather than from the predicate would not
have noticed that. The scan itself is exercised here too, because the
narrow loop it runs is inlined for speed and the predicate cannot check it.
"""

from __future__ import annotations

import importlib
import shutil
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlmodel import Session

from tests.conftest import (
    _REPOSITORY_MODULES,
    is_repository_module,
    repair_captured_session,
)

ROOT = Path(__file__).resolve().parents[2]


def repository_package_names(isolated_git) -> set[str]:
    """The top-level package names the repository owns.

    Enumerated from the *tracked* files, not from the filesystem: a
    gitignored directory that happens to hold a ``.py`` file -- ``coverage/``
    and ``htmlcov/`` from ``make test-cov``, ``data/`` from a local run, a
    leftover ``venv/`` -- is not a package of this repository, and the
    filesystem cannot tell the difference.
    """
    if shutil.which("git") is None:
        pytest.skip("git is required to enumerate the tracked sources")
    listed = isolated_git("ls-files", "--", "*.py", cwd=ROOT)
    if listed.returncode != 0 or not listed.stdout.split():
        pytest.skip("not a git checkout - there is nothing to assert")
    names = set()
    for path in listed.stdout.split():
        head, sep, _ = path.partition("/")
        if sep:
            names.add(head)
    return names


def test_the_scan_covers_every_package_the_repository_owns(isolated_git) -> None:
    """A new top-level package has to be added to the scan's roots.

    Only a module that reads ``sqlmodel.Session`` at runtime can capture the
    mock, and the only ones that do are the ones this repository owns -- so
    the scan skips everything else. A package outside the roots would be
    skipped without anyone noticing.

    The expectation is derived from the scan's own predicate, not from the
    root list: the two have to be the same thing. A predicate that stopped
    matching the package roots fails here, instead of quietly skipping them.
    """
    owned = repository_package_names(isolated_git)
    assert owned, "the repository owns no top-level package?"
    covered = {root for root in _REPOSITORY_MODULES if is_repository_module(root)}
    assert owned <= covered, sorted(owned - covered)


def test_the_scan_matches_the_package_roots_and_not_only_their_modules() -> None:
    """The package itself is matched, not only the modules inside it.

    ``sys.modules["app"]`` is the package: a module that reads
    ``sqlmodel.Session`` in ``app/__init__.py`` captures the mock *there*,
    and a scan that only matched ``app.something`` would leave it behind.
    """
    assert is_repository_module("app")
    assert is_repository_module("app.models")
    assert not is_repository_module("application")


def test_the_scan_repairs_a_capture_on_a_package_root() -> None:
    """The scan itself, not only the predicate the guard shares with it.

    The scan is what runs at the end of every test, so it is exercised
    here rather than through the predicate: the two read the same
    constants, but only this test would notice if the *loop* stopped
    matching the package roots -- and the loop is what the predicate
    cannot check.

    The narrow loop is inlined for speed, so this is the one place where
    the guarantee is measured rather than assumed.
    """
    module = importlib.import_module("app")
    had = "Session" in vars(module)
    before = vars(module).get("Session")
    mock_session = MagicMock()
    module.Session = mock_session
    try:
        repair_captured_session(mock_session, Session)
        assert module.Session is Session
    finally:
        if had:
            module.Session = before
        else:
            del module.Session


def test_the_scan_covers_the_module_the_fixture_patches() -> None:
    """``sqlmodel`` is in the scan.

    It is the module the fixture patches, so ``monkeypatch`` restores it and
    the scan does not have to -- but it is in the list so that a capture
    *inside* ``sqlmodel`` would still be repaired.
    """
    assert is_repository_module("sqlmodel")


def test_no_module_holds_a_mocked_session() -> None:
    """The mocked ``Session`` did not outlive the test that captured it.

    ``sqlmodel`` holds the mock for the duration of every test, and
    ``monkeypatch`` restores it afterwards. Any *other* module holding a
    mocked ``Session`` is a leak: the scan in ``tests/conftest.py`` repairs
    the ones this repository owns, and this test is what catches the rest.

    It is a point-in-time sample, not an end-to-end guard: it sees the
    modules that are loaded when it runs. A leak injected by a test that
    sorts after this file is not caught here -- the scan in the fixture is
    what repairs it, and this test is the belt to that braces.
    """
    leaked = [
        name
        for name, module in list(sys.modules.items())
        if module is not None
        and name != "sqlmodel"
        and isinstance(vars(module).get("Session"), MagicMock)
    ]
    assert not leaked, leaked
