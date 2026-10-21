"""Release image signing retries only a transient OIDC token fault."""

import subprocess
import sys

import pytest

from scripts import sign_release_image

REFERENCE = "ghcr.io/s3ntin3l8/runway@sha256:" + "a" * 64
TRANSIENT = (
    "retrieving ID token: reading ID token: fetching ambient OIDC credentials: "
    "invalid character 'u' looking for beginning of value"
)


def _completed(stderr: str, code: int = 1) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["cosign", "sign"], code, "", stderr)


@pytest.mark.parametrize("failures", [1, 2])
def test_transient_token_fault_is_retried(monkeypatch, failures):
    calls = []

    def run(command, **kwargs):
        assert command == ["cosign", "sign", "--yes", REFERENCE]
        calls.append(command)
        if len(calls) <= failures:
            return _completed(TRANSIENT)
        return _completed("", code=0)

    monkeypatch.setattr(sign_release_image.subprocess, "run", run)
    monkeypatch.setattr(sign_release_image.time, "sleep", lambda _: None)
    sign_release_image.sign_image(REFERENCE)
    assert len(calls) == failures + 1


def test_a_permanent_failure_is_not_retried(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return _completed("error: OIDC permission denied")

    monkeypatch.setattr(sign_release_image.subprocess, "run", run)
    monkeypatch.setattr(sign_release_image.time, "sleep", lambda _: None)
    with pytest.raises(RuntimeError, match="signing failed"):
        sign_release_image.sign_image(REFERENCE)
    assert len(calls) == 1


def test_a_persistent_transient_fault_still_fails_closed(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return _completed(TRANSIENT)

    monkeypatch.setattr(sign_release_image.subprocess, "run", run)
    monkeypatch.setattr(sign_release_image.time, "sleep", lambda _: None)
    with pytest.raises(RuntimeError, match="signing failed"):
        sign_release_image.sign_image(REFERENCE)
    assert len(calls) == sign_release_image._ATTEMPTS


def test_a_successful_sign_does_not_retry(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return _completed("", code=0)

    monkeypatch.setattr(sign_release_image.subprocess, "run", run)
    sign_release_image.sign_image(REFERENCE)
    assert len(calls) == 1


def test_transient_detection_ignores_unrelated_output():
    assert sign_release_image.is_transient(TRANSIENT)
    assert not sign_release_image.is_transient("error: no identity token provided")
    assert not sign_release_image.is_transient("")


def test_transient_detection_requires_the_ambient_credential_markers():
    # Both markers must be present: "invalid character" on its own is some
    # other tool's JSON error, and "fetching ambient OIDC credentials" on
    # its own can be a permanent rejection from the token endpoint.
    assert not sign_release_image.is_transient(
        "invalid character 'u' looking for beginning of value"
    )
    assert not sign_release_image.is_transient("fetching ambient OIDC credentials: 403 Forbidden")


def test_a_missing_cosign_fails_closed(monkeypatch):
    def run(command, **kwargs):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(sign_release_image.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="cosign is not installed"):
        sign_release_image.sign_image(REFERENCE)


def test_cosign_output_is_echoed_to_the_log(monkeypatch, capsys):
    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, "signed\n", "")

    monkeypatch.setattr(sign_release_image.subprocess, "run", run)
    sign_release_image.sign_image(REFERENCE)
    assert "signed" in capsys.readouterr().out


def test_main_annotates_a_failure(monkeypatch, capsys):
    monkeypatch.setattr(
        sign_release_image,
        "sign_image",
        lambda reference: (_ for _ in ()).throw(RuntimeError("signing failed")),
    )
    monkeypatch.setattr(sys, "argv", ["sign_release_image", REFERENCE])
    with pytest.raises(SystemExit) as exc:
        sign_release_image.main()
    assert exc.value.code == 1
    assert "::error::" in capsys.readouterr().err


def test_the_backoff_escalates_and_stays_bounded(monkeypatch):
    sleeps = []
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return _completed(TRANSIENT)

    monkeypatch.setattr(sign_release_image.subprocess, "run", run)
    monkeypatch.setattr(sign_release_image.time, "sleep", sleeps.append)
    with pytest.raises(RuntimeError, match="signing failed"):
        sign_release_image.sign_image(REFERENCE)
    # One attempt, then one sleep per remaining attempt: the total wait is
    # bounded, and it grows rather than repeating a fixed delay.
    assert len(calls) == sign_release_image._ATTEMPTS
    assert len(sleeps) == sign_release_image._ATTEMPTS - 1
    assert sleeps == sorted(sleeps)
    assert sum(sleeps) <= 60
