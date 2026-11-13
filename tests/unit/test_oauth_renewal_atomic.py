"""atomic_replace_json: replace a credential file in place without changing its shape."""

import json
import os
from pathlib import Path

from scripts.sidecar_pkg import oauth_renewal


def test_writes_lf_line_endings(tmp_path: Path) -> None:
    """A renewal must not turn the CLI's LF file into CRLF (text mode does so on Windows)."""
    target = tmp_path / "auth.json"
    target.write_bytes(b'{\n  "a": 1\n}')

    assert oauth_renewal.atomic_replace_json(target, {"a": 2, "b": [1, 2]}, 0o600)

    raw = target.read_bytes()
    assert b"\r" not in raw
    assert raw.count(b"\n") > 1  # indented, so the check above is not vacuous
    assert json.loads(raw) == {"a": 2, "b": [1, 2]}


def test_opens_the_temp_file_with_an_explicit_lf_newline(tmp_path: Path, monkeypatch) -> None:
    """On POSIX the default already writes LF, so pin the argument that matters on Windows."""
    seen: dict[str, object] = {}
    real = os.fdopen

    def spy(fd, *args, **kwargs):
        seen.update(kwargs)
        return real(fd, *args, **kwargs)

    monkeypatch.setattr(oauth_renewal.os, "fdopen", spy)
    target = tmp_path / "auth.json"
    target.write_text("{}")

    assert oauth_renewal.atomic_replace_json(target, {"a": 1}, 0o600)
    assert seen.get("newline") == "\n"


def test_keeps_the_mode_and_leaves_no_temp_file(tmp_path: Path) -> None:
    target = tmp_path / "auth.json"
    target.write_text("{}")

    assert oauth_renewal.atomic_replace_json(target, {"a": 1}, 0o600)

    assert [p.name for p in tmp_path.iterdir()] == ["auth.json"]
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o600
