"""Shared helpers and fixtures for sidecar credentials unit tests."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.macos_cookie_fixtures import approved_keychain_readers  # noqa: F401

__all__ = ["_ok_response", "approved_keychain_readers", "isolate_host_credentials"]


def _ok_response(status: int = 200, body: bytes | None = None) -> MagicMock:
    resp = MagicMock()
    resp.getcode = MagicMock(return_value=status)
    if status == 200 and body is not None:
        resp.read = MagicMock(return_value=body)
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
    else:
        # For error path, .read() raises HTTPError
        from urllib import error

        resp.read = MagicMock(
            side_effect=error.HTTPError(
                url="x", code=status, msg="err", hdrs={}, fp=MagicMock(read=lambda: b"")
            )
        )
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
    return resp


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
