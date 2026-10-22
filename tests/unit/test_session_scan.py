"""The suite's mocked ``Session`` must not outlive the test that captured it.

``tests/conftest.py`` patches ``sqlmodel.Session`` with a mock for every
test. A module that reads ``sqlmodel.Session`` at runtime captures that mock,
and it would then outlive the test -- so the fixture walks the modules this
repository owns and restores the real ``Session`` in any of them that still
holds the mock.

The walk is the expensive part, so it is narrowed to the modules that can
capture it. This file pins that narrowing: a top-level package the
repository owns and the scan does not know about would be skipped silently.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

from tests.conftest import _REPOSITORY_MODULES

ROOT = Path(__file__).resolve().parents[2]
SKIP = {".git", ".venv", "node_modules", ".wt", "dist", "build", "__pycache__"}


def repository_package_names() -> set[str]:
    """The top-level directory names that hold Python modules of this repository."""
    names: set[str] = set()
    for entry in sorted(ROOT.iterdir()):
        if not entry.is_dir() or entry.name in SKIP or entry.name.startswith("."):
            continue
        for _dirpath, dirnames, filenames in os.walk(entry):
            dirnames[:] = [d for d in dirnames if d not in SKIP and not d.startswith(".")]
            if any(name.endswith(".py") for name in filenames):
                names.add(entry.name)
                break
    return names


def test_the_scan_covers_every_package_the_repository_owns() -> None:
    """A new top-level package has to be added to the scan's prefixes.

    Only a module that reads ``sqlmodel.Session`` at runtime can capture the
    mock, and the only ones that do are the ones this repository owns -- so
    the scan skips everything else. A package outside the prefixes would be
    skipped without anyone noticing.
    """
    owned = repository_package_names()
    assert owned, "the repository owns no top-level package?"
    covered = {prefix.rstrip(".") for prefix in _REPOSITORY_MODULES}
    assert owned <= covered, sorted(owned - covered)


def test_the_scan_covers_the_module_the_fixture_patches() -> None:
    """``sqlmodel`` is in the scan.

    It is the module the fixture patches, so ``monkeypatch`` restores it and
    the scan does not have to -- but it is in the list so that a capture
    *inside* ``sqlmodel`` would still be repaired.
    """
    assert "sqlmodel" in {prefix.rstrip(".") for prefix in _REPOSITORY_MODULES}


def test_no_module_holds_a_mocked_session() -> None:
    """The mocked ``Session`` did not outlive the test that captured it.

    ``sqlmodel`` holds the mock for the duration of every test, and
    ``monkeypatch`` restores it afterwards. Any *other* module holding a
    mocked ``Session`` is a leak: the scan in ``tests/conftest.py`` repairs
    the ones this repository owns, and this test is what catches the rest.
    """
    leaked = [
        name
        for name, module in list(sys.modules.items())
        if module is not None
        and name != "sqlmodel"
        and isinstance(vars(module).get("Session"), MagicMock)
    ]
    assert not leaked, leaked
