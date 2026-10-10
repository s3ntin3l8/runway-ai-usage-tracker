"""Adversarial streams must be bounded before any installation changes."""

import io
from unittest.mock import Mock

import pytest

from scripts import check_update_asset_sizes as sizes
from scripts.sidecar_pkg import self_update, signatures, update_limits


class Response(io.BytesIO):
    def __init__(self, data, declared=None):
        super().__init__(data)
        self.headers = {} if declared is None else {"Content-Length": declared}


@pytest.mark.parametrize("declared", [None, "0", "1", "-1", "invalid", "10"])
def test_actual_bytes_override_content_length(declared):
    assert update_limits.bounded_read(Response(b"x" * 10, declared), 10) == b"x" * 10
    with pytest.raises(update_limits.UpdateSizeError):
        update_limits.bounded_read(Response(b"x" * 11, declared), 10)


def test_oversized_declared_length_rejected_before_read():
    response = Response(b"x", "11")
    response.read = Mock(side_effect=AssertionError("must not read"))
    with pytest.raises(update_limits.UpdateSizeError):
        update_limits.bounded_read(response, 10)


def test_multi_chunk_stream_never_yields_overflow(monkeypatch):
    monkeypatch.setattr(update_limits, "CHUNK_BYTES", 3)
    chunks = update_limits.bounded_chunks(Response(b"x" * 11), 10)
    assert [next(chunks) for _ in range(3)] == [b"xxx"] * 3
    with pytest.raises(update_limits.UpdateSizeError):
        next(chunks)


def test_overflow_closes_response_and_removes_partial_file(tmp_path, monkeypatch):
    response = Response(b"x" * 11)
    monkeypatch.setattr(update_limits, "CHUNK_BYTES", 3)
    monkeypatch.setattr(self_update.request, "urlopen", lambda *args, **kwargs: response)
    monkeypatch.setattr(self_update, "_github_ssl_context", lambda _: None)
    dest = tmp_path / "payload.zip"
    with pytest.raises(update_limits.UpdateSizeError):
        self_update._with_retries(
            lambda: self_update._download("https://example.test/x", dest, max_bytes=10), what="test"
        )
    assert response.closed
    assert not dest.exists()


def test_interrupted_download_removes_partial_file(tmp_path, monkeypatch):
    response = Response(b"abc")
    response.read = Mock(side_effect=[b"abc", OSError("interrupted")])
    monkeypatch.setattr(self_update.request, "urlopen", lambda *args, **kwargs: response)
    monkeypatch.setattr(self_update, "_github_ssl_context", lambda _: None)
    dest = tmp_path / "payload.zip"
    with pytest.raises(OSError):
        self_update._download("https://example.test/x", dest)
    assert response.closed and not dest.exists()


def test_exact_limit_download_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(self_update.request, "urlopen", lambda *args, **kwargs: Response(b"x" * 10))
    monkeypatch.setattr(self_update, "_github_ssl_context", lambda _: None)
    dest = tmp_path / "payload.zip"
    self_update._download("https://example.test/x", dest, max_bytes=10)
    assert dest.read_bytes() == b"x" * 10


def test_local_archive_rejected_before_hashing(tmp_path, monkeypatch):
    archive = tmp_path / "archive.zip"
    archive.write_bytes(b"x" * 11)
    monkeypatch.setattr(self_update, "MAX_ARCHIVE_BYTES", 10)
    monkeypatch.setattr(
        self_update.hashlib, "sha256", Mock(side_effect=AssertionError("must not hash"))
    )
    with pytest.raises(update_limits.UpdateSizeError):
        self_update.verify_sha256(archive, "a" * 64)
    monkeypatch.setattr(signatures, "MAX_ARCHIVE_BYTES", 10)
    with pytest.raises(signatures.UpdateVerificationError):
        signatures.verify_update(archive, tmp_path / "absent.bundle")


def test_overflow_never_extracts_installs_restarts_or_retries(tmp_path, monkeypatch):
    installed = tmp_path / "installed.exe"
    previous = tmp_path / "installed.exe.previous"
    installed.write_bytes(b"current install")
    previous.write_bytes(b"rollback install")
    monkeypatch.setattr(self_update, "_sidecar_dir", lambda: tmp_path)
    monkeypatch.setattr(self_update, "_is_frozen", lambda: True)
    monkeypatch.setattr(self_update, "_is_docker", lambda: False)
    monkeypatch.setattr(self_update, "running_from_disk_image", lambda: False)
    monkeypatch.setattr(self_update, "_detect_target", lambda: "cli")
    monkeypatch.setattr(self_update, "check_once", lambda *args: "2.0.0")
    name = self_update.resolve_asset_name("cli", "stable", "v2.0.0")
    monkeypatch.setattr(
        self_update,
        "_get_release_json",
        lambda _: {
            "tag_name": "v2.0.0",
            "assets": [
                {"name": n, "browser_download_url": "https://example.test/" + n}
                for n in (name, name + ".sha256", name + ".sigstore.json")
            ],
        },
    )
    calls = []

    def oversized(*args, **kwargs):
        calls.append(args)
        raise update_limits.UpdateSizeError("Update resource exceeds size limit")

    monkeypatch.setattr(self_update, "_download", oversized)
    for function in ("_extract", "apply_update"):
        monkeypatch.setattr(
            self_update, function, Mock(side_effect=AssertionError("must not mutate"))
        )
    assert self_update.self_update("1.0.0", "stable", restart=True) is False
    assert len(calls) == 1
    assert installed.read_bytes() == b"current install"
    assert previous.read_bytes() == b"rollback install"
    assert not (tmp_path / self_update._LOCK_NAME).exists()


def test_packaging_budget_checks(tmp_path, monkeypatch):
    monkeypatch.setattr(sizes, "MAX_ARCHIVE_BYTES", 10)
    monkeypatch.setattr(sizes, "MAX_BUNDLE_BYTES", 5)
    assert sizes.check_sizes(tmp_path)
    payload = tmp_path / "Runway-Sidecar-Windows-edge.zip"
    bundle = payload.with_name(payload.name + ".sigstore.json")
    payload.write_bytes(b"x" * 10)
    bundle.write_bytes(b"x" * 5)
    assert sizes.check_sizes(tmp_path) == []
    payload.write_bytes(b"x" * 11)
    bundle.write_bytes(b"x" * 6)
    assert len(sizes.check_sizes(tmp_path)) == 2
