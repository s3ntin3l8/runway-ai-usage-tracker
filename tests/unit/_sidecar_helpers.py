"""Shared helpers and fixtures for sidecar unit tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture(autouse=True)
def isolate_host_credentials(monkeypatch, tmp_path):
    """Synthetic tests must never inspect host browser stores or Keychain."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    original_run = subprocess.run

    def guarded_run(command, *args, **kwargs):
        if (
            isinstance(command, (list, tuple))
            and command
            and Path(str(command[0])).name == "security"
        ):
            raise AssertionError("Live Keychain access is forbidden in unit tests")
        return original_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded_run)


class _StubCache:
    """Minimal stand-in for ``CredentialCache`` so the test can stub
    ``_get_credential_cache`` without dragging in the full cache.

    Only ``is_fresh()`` and ``provider_accounts()`` / ``provider_tag_hints()``
    are exercised by ``run_collection`` in this test path."""

    def is_fresh(self, *, now=None):
        return True  # Skip the fetch path; the test supplies its own.

    def provider_accounts(self):
        return {}

    def provider_tag_hints(self):
        return {}
