"""Shared helpers and fixtures for sidecar credentials unit tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.macos_cookie_fixtures import approved_keychain_readers  # noqa: F401

__all__ = ["approved_keychain_readers", "isolate_host_credentials"]


@pytest.fixture(autouse=True)
def isolate_host_credentials(monkeypatch, tmp_path):
    """Synthetic tests must never inspect host browser stores or Keychain."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    original_run = subprocess.run

    def guarded_run(command, *args, **kwargs):
        if (
            isinstance(command, list | tuple)
            and command
            and Path(str(command[0])).name == "security"
        ):
            raise AssertionError("Live Keychain access is forbidden in unit tests")
        return original_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded_run)
