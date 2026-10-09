"""Secrets must not sit in plaintext queues, logs, error bodies or token files (#446)."""

import asyncio
import hashlib
import hmac
import json
import logging
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, create_engine, select
from sqlmodel.orm.session import Session
from sqlmodel.pool import StaticPool

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.core.db import get_session  # noqa: E402
from app.core.encryption import EncryptionService  # noqa: E402
from app.main import app  # noqa: E402
from app.models.db import SidecarRegistry  # noqa: E402
from scripts import sidecar  # noqa: E402

KEY = "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdef"  # pragma: allowlist secret
OAUTH = "ya29.a0AfH6SMBsyntheticaccesstokenvalue1234567890"  # pragma: allowlist secret
INGEST_KEY = "test-secrets-at-rest-ingest-key"  # pragma: allowlist secret
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghij"  # pragma: allowlist secret
OPAQUE = "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0"  # pragma: allowlist secret


def _token_card(**extra):
    return {
        "service_name": "Claude Pro",
        "remaining": "Token",
        "unit": "oauth",
        "metadata": {"provider_id": "anthropic", "oauth_token": OAUTH, "refresh_token": KEY},
        **extra,
    }


def _quota_card():
    return {"service_name": "Claude Pro", "remaining": "80%", "unit": "capacity", "metadata": {}}


# --- sidecar offline queue -----------------------------------------------------


def test_strip_credentials_keeps_usage_and_drops_credential_cards():
    payload = {
        "provider": "x",
        "metrics": [
            _token_card(),
            _quota_card(),
            _token_card(unit="api_key"),
            _token_card(unit="cookie"),
        ],
        "events": [{"ts": "2026-01-01T00:00:00Z"}],
    }

    stripped = sidecar.strip_credentials(payload)

    assert stripped["metrics"] == [_quota_card()]
    assert stripped["events"] == payload["events"]
    assert len(payload["metrics"]) == 4, "the input is not mutated"


def test_strip_credentials_leaves_other_shapes_alone():
    assert sidecar.strip_credentials({"a": 1}) == {"a": 1}


def test_strip_credentials_redacts_forwarded_log_lines():
    stripped = sidecar.strip_credentials({"last_log_lines": [f"api_key={KEY}", "fine"]})
    assert KEY not in json.dumps(stripped)
    assert "fine" in stripped["last_log_lines"]


@pytest.fixture
def queue_dirs(tmp_path, monkeypatch):
    sidecar_dir = tmp_path / "sidecar"
    queue_dir = sidecar_dir / "queue"
    monkeypatch.setattr(sidecar, "get_sidecar_dir", lambda: sidecar_dir)
    monkeypatch.setattr(sidecar, "get_queue_dir", lambda: queue_dir)
    return queue_dir


@pytest.mark.skipif(sys.platform == "win32", reason="Unix queue permission semantics")
def test_queued_payload_holds_no_credentials(queue_dirs):
    payload = {"metrics": [_token_card(), _quota_card()], "events": []}

    assert sidecar.queue_push(payload) is True

    text = "".join(f.read_text() for f in queue_dirs.glob("*.jsonl"))
    assert OAUTH not in text and KEY not in text
    assert "80%" in text, "usage is still queued"


@pytest.mark.skipif(sys.platform == "win32", reason="Unix queue permission semantics")
def test_flush_drops_credential_only_entries_from_older_sidecars(queue_dirs, monkeypatch):
    queue_dirs.mkdir(parents=True)
    (queue_dirs / "2026-01-01.jsonl").write_text(
        json.dumps({"ts": 1, "payload": {"metrics": [_token_card()]}}) + "\n"
    )
    sent = []
    monkeypatch.setattr(
        sidecar,
        "http_post_signed_with_retry",
        lambda url, payload, *a, **k: sent.append(payload) or (True, {}, 200),
    )

    sidecar.queue_flush("http://localhost", "k")

    assert sent == [], "a stale credential is not worth replaying"
    assert not list(queue_dirs.glob("*.jsonl"))


@pytest.mark.skipif(sys.platform == "win32", reason="Unix queue permission semantics")
def test_failed_replay_rewrites_the_entry_without_its_credentials(queue_dirs, monkeypatch):
    queue_dirs.mkdir(parents=True)
    legacy = {"ts": 1, "payload": {"metrics": [_token_card(), _quota_card()], "events": []}}
    (queue_dirs / "2026-01-01.jsonl").write_text(json.dumps(legacy) + "\n")
    monkeypatch.setattr(
        sidecar, "http_post_signed_with_retry", lambda *a, **k: (False, 0, "offline")
    )

    sidecar.queue_flush("http://localhost", "k")

    text = (queue_dirs / "2026-01-01.jsonl").read_text()
    assert OAUTH not in text and KEY not in text
    assert "80%" in text


# --- log redaction -------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        f"Failed to send metrics (HTTP 400): validation error input_value='{KEY}', input_type=str",
        f"Authorization: Bearer {OAUTH}",
        f'{{"access_token": "{OAUTH}", "x": 1}}',
        f"cookie_session={OAUTH}; other=1",
        f"jwt {JWT}",
        f"key {KEY}",
        f"opaque {OPAQUE}",
    ],
)
def test_redact_log_text_scrubs_credential_shapes(line):
    out = sidecar.redact_log_text(line)
    for secret in (
        KEY,
        OAUTH,
        "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0",
        "eyJzdWIi",
        "abcdefghij",
    ):
        assert secret not in out


@pytest.mark.parametrize(
    "line",
    [
        "tokens: 120 used",
        "session_id=77",
        "  [anthropic] token file matched: /home/u/.claude/.credentials.json",
        "POST http://server/api/v1/fleet/ingest 200 in 0.3s",
    ],
)
def test_redact_log_text_leaves_ordinary_lines_alone(line):
    assert sidecar.redact_log_text(line) == line


@pytest.mark.parametrize(
    "line",
    [
        "token=abc123",
        '{"api_key":"short"}',
        "{'access_token': 'abcdef0123456789'}",
        "cookie_session=short; x=1",
        "running with --api-key abc123def456",
        "--token=abc123",
    ],
)
def test_redact_log_text_scrubs_short_secrets_by_name(line):
    """Short values are only caught by name; the length/prefix patterns miss them."""
    out = sidecar.redact_log_text(line)
    assert "abc123" not in out and "short" not in out and "abcdef0123456789" not in out


@pytest.mark.parametrize(
    "line",
    ["a" * 20000, "x" * 20000 + " token ", "a-" * 10000, "token" * 5000],
)
def test_redaction_is_fast_on_hostile_lines(line):
    """The patterns run on every log record; they must not backtrack quadratically."""
    from app.core.log_redaction import redact_secrets

    benign = "ab " * (len(line) // 3)  # same length, nothing to match
    start = time.perf_counter()
    sidecar.redact_log_text(benign)
    redact_secrets(benign[:2000])
    baseline = time.perf_counter() - start

    start = time.perf_counter()
    sidecar.redact_log_text(line)
    redact_secrets(line[:2000])
    hostile = time.perf_counter() - start

    # Relative, so a slow CI runner doesn't flake it; the quadratic version was
    # thousands of times slower than the benign line, not a small multiple.
    assert hostile < max(0.5, baseline * 50)


def test_logging_filter_redacts_formatted_messages():
    record = logging.LogRecord(
        "t", logging.ERROR, __file__, 1, "send failed: %s", (f"token={OAUTH}",), None
    )

    assert sidecar._RedactingFilter().filter(record) is True

    assert OAUTH not in record.getMessage()


def test_logging_filter_redacts_tracebacks_too(tmp_path):
    """exc_info=True call sites must not write a secret-bearing traceback to the log."""
    log = tmp_path / "x.log"
    handler = logging.FileHandler(log)
    handler.addFilter(sidecar._RedactingFilter())
    logger = logging.getLogger("secrets-at-rest-traceback-test")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        try:
            raise ValueError(f"ingest rejected: input_value='{OAUTH}'")
        except ValueError:
            logger.error("send failed", exc_info=True)
    finally:
        logger.removeHandler(handler)
        handler.close()

    text = log.read_text()
    assert OAUTH not in text
    assert "ValueError" in text and "send failed" in text


def test_log_tail_is_redacted_even_for_lines_written_by_older_versions(tmp_path, monkeypatch):
    log = tmp_path / "sidecar.log"
    log.write_text(f"old line leaking api_key={KEY}\nplain\n")
    monkeypatch.setattr(sidecar, "get_log_path", lambda: log)

    tail = sidecar._tail_log(5)

    assert KEY not in "".join(tail)
    assert tail[-1] == "plain"


# --- server: error bodies and stored log tail ------------------------------------


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        app.dependency_overrides[get_session] = lambda: s
        yield s
        app.dependency_overrides.pop(get_session, None)


def _signed(payload: dict) -> tuple[bytes, dict]:
    body = json.dumps(payload, separators=(",", ":")).encode()
    ts = str(time.time())
    sig = hmac.new(INGEST_KEY.encode(), ts.encode() + body, hashlib.sha256).hexdigest()
    return body, {"X-Signature": sig, "X-Timestamp": ts, "Content-Type": "application/json"}


# Short enough that Pydantic's own truncation of echoed input can't hide a leak.
SHORT_SECRET = "sk-test-0123456789abcdef"  # pragma: allowlist secret


@pytest.mark.parametrize(
    ("route", "payload"),
    [
        # `metrics` must be a list; the secret rides in the wrongly typed value.
        ("/api/v1/fleet/ingest", {"provider": "x", "metrics": SHORT_SECRET}),
        ("/api/v1/fleet/credentials/manifest", {"sidecar_id": "h", "entries": SHORT_SECRET}),
    ],
)
def test_validation_errors_never_echo_the_submitted_value(db, caplog, route, payload):
    body, headers = _signed(payload)
    with patch("app.core.config.settings") as mock_settings, caplog.at_level(logging.DEBUG):
        mock_settings.INGEST_API_KEY = INGEST_KEY
        mock_settings.INGEST_API_KEY_IS_INSECURE_DEFAULT = False
        resp = TestClient(app).post(route, content=body, headers=headers)

    assert resp.status_code == 400
    assert SHORT_SECRET not in resp.text
    assert SHORT_SECRET not in caplog.text
    assert "Invalid" in resp.json()["detail"]


def test_stored_recent_logs_are_redacted(db):
    from app.services.fleet_registry import fleet_registry

    fleet_registry.upsert_sidecar(
        "host-1",
        "127.0.0.1",
        db,
        last_log_lines=[f"api_key={KEY}", f"Authorization: Bearer {OAUTH}", "ok"],
    )
    db.commit()
    stored = db.exec(select(SidecarRegistry)).one().recent_logs

    assert KEY not in stored and OAUTH not in stored
    assert "ok" in json.loads(stored)

    fleet_registry.upsert_sidecar("host-1", "127.0.0.1", db, last_log_lines=[f"token={OAUTH}"])
    db.commit()
    db.expire_all()
    assert OAUTH not in db.exec(select(SidecarRegistry)).one().recent_logs


# --- GitHub OAuth token at rest ----------------------------------------------------


@pytest.fixture
def github_file(tmp_path, monkeypatch):
    from app.api.endpoints import github_oauth

    path = tmp_path / "runway" / "github_oauth.json"
    monkeypatch.setattr(github_oauth.settings, "GITHUB_OAUTH_PATH", str(path))
    return path


@pytest.fixture
def encryption(monkeypatch):
    service = EncryptionService(key=Fernet.generate_key().decode())
    monkeypatch.setattr("app.api.endpoints.github_oauth.encryption_service", service)
    monkeypatch.setattr("app.services.credential_provider.encryption_service", service)
    return service


def test_github_token_is_encrypted_on_disk_and_round_trips(github_file, encryption):
    from app.api.endpoints import github_oauth

    asyncio.run(
        github_oauth.save_token({"access_token": "gho_synthetic_token_value", "login": "a"})
    )

    on_disk = github_file.read_text()
    assert "gho_synthetic_token_value" not in on_disk
    assert json.loads(on_disk)["login"] == "a", "only the secret is encrypted"
    assert github_oauth.load_token()["access_token"] == "gho_synthetic_token_value"


def test_legacy_plaintext_github_token_is_still_readable(github_file, encryption):
    from app.api.endpoints import github_oauth

    github_file.parent.mkdir(parents=True)
    github_file.write_text(json.dumps({"access_token": "gho_legacy_plain_token"}))

    assert github_oauth.load_token()["access_token"] == "gho_legacy_plain_token"
    assert github_oauth._token_stored_in_plaintext() is True


def test_get_status_upgrades_a_plaintext_token_in_place(github_file, encryption, monkeypatch):
    from app.api.endpoints import github_oauth

    github_file.parent.mkdir(parents=True)
    github_file.write_text(json.dumps({"access_token": "gho_legacy_plain_token", "login": "a"}))

    class _Resp:
        status_code = 401
        text = ""

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(github_oauth.httpx, "AsyncClient", lambda *a, **k: _Client())

    status = asyncio.run(github_oauth.get_status())

    assert status.authenticated is True
    assert "gho_legacy_plain_token" not in github_file.read_text()
    assert github_oauth.load_token()["access_token"] == "gho_legacy_plain_token"


def test_without_a_key_the_token_stays_plaintext_and_nothing_breaks(github_file, monkeypatch):
    from app.api.endpoints import github_oauth

    monkeypatch.setattr(github_oauth, "encryption_service", EncryptionService(key=None))

    asyncio.run(github_oauth.save_token({"access_token": "gho_plain_when_no_key"}))

    assert "gho_plain_when_no_key" in github_file.read_text()
    assert github_oauth.load_token()["access_token"] == "gho_plain_when_no_key"
    assert github_oauth._token_stored_in_plaintext() is False


def test_server_credential_scan_finds_the_decrypted_github_token(
    github_file, encryption, monkeypatch
):
    from app.api.endpoints import github_oauth
    from app.services.credential_provider import CredentialProvider

    asyncio.run(github_oauth.save_token({"access_token": "gho_scan_me_token"}))
    monkeypatch.setattr(
        "app.services.credential_provider.get_platform_config_dir", lambda _name: github_file.parent
    )
    monkeypatch.setattr(
        "app.services.credential_provider._expand_rule_paths", lambda paths: [str(github_file)]
    )
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    creds = CredentialProvider.get_credentials("github")

    assert creds.get("api_key") == "gho_scan_me_token"
    assert creds.sources.get("api_key") == "config"


def test_an_undecryptable_github_token_is_skipped_not_returned_as_ciphertext(
    github_file, encryption, monkeypatch
):
    from app.api.endpoints import github_oauth
    from app.services.credential_provider import CredentialProvider

    asyncio.run(github_oauth.save_token({"access_token": "gho_scan_me_token"}))
    other = EncryptionService(key=Fernet.generate_key().decode())
    monkeypatch.setattr("app.services.credential_provider.encryption_service", other)
    monkeypatch.setattr(
        "app.services.credential_provider.get_platform_config_dir", lambda _name: github_file.parent
    )
    monkeypatch.setattr(
        "app.services.credential_provider._expand_rule_paths", lambda paths: [str(github_file)]
    )
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    creds = CredentialProvider.get_credentials("github")

    assert not creds.get("api_key")


def test_ciphertext_is_never_used_as_a_token_once_the_key_is_gone(
    github_file, encryption, monkeypatch
):
    from app.api.endpoints import github_oauth
    from app.services.credential_provider import CredentialProvider

    asyncio.run(github_oauth.save_token({"access_token": "gho_scan_me_token"}))
    keyless = EncryptionService(key=None)
    monkeypatch.setattr("app.api.endpoints.github_oauth.encryption_service", keyless)
    monkeypatch.setattr("app.services.credential_provider.encryption_service", keyless)
    monkeypatch.setattr(
        "app.services.credential_provider.get_platform_config_dir", lambda _name: github_file.parent
    )
    monkeypatch.setattr(
        "app.services.credential_provider._expand_rule_paths", lambda paths: [str(github_file)]
    )
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    with pytest.raises(ValueError, match="encrypted"):
        github_oauth.load_token()
    assert not CredentialProvider.get_credentials("github").get("api_key")


def test_the_sidecar_does_not_ship_runways_own_github_token_file():
    paths = [
        p
        for rule in sidecar.__REGISTRY__["providers"]["github"]["rules"]
        for p in rule.get("paths", [])
    ]
    assert not any("github_oauth.json" in p for p in paths)


def test_github_migration_encryption_failure_returns_503_and_preserves_file(
    github_file, encryption
):
    github_file.parent.mkdir(parents=True)
    original = json.dumps({"access_token": "legacy-test-token"})
    github_file.write_text(original)
    with patch.object(encryption._fernet, "encrypt", side_effect=RuntimeError("synthetic failure")):
        response = TestClient(app).get("/api/v1/auth/github/status")
    assert response.status_code == 503
    assert "refusing to persist plaintext" in response.json()["detail"]
    assert "synthetic failure" not in response.text
    assert github_file.read_text() == original
