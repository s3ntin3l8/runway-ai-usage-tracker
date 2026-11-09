"""Integration tests for sidecar ingest reconciliation, fingerprint rotation, and phantom accounts."""

from __future__ import annotations

import json
import time

import pytest
from sqlmodel import Session, select

from app.models.db import CredentialSource
from tests.fixtures.test_client import TestClient
from tests.integration._fleet_credentials_helpers import (
    _add_provider_config,
    _file_source,
    _ingest,
    _isolated_ingest_key,  # noqa: F401 — fixture
    _post_manifest,
    _sign,
    _token_card,
    client_fixture,  # noqa: F401 — fixture
    session_fixture,  # noqa: F401 — fixture
)

# --- rotation carry (#474) -------------------------------------------------


def _rotation_origins() -> tuple[str, str]:
    base = "path:/home/alice/.local/share/opencode/auth.json"
    return f"{base}#0123456789ab", f"{base}#abcdef012345"


def test_ingest_reports_events_error_when_event_storage_fails(
    client: TestClient, session: Session
) -> None:
    """HTTP 200 with ``events_error`` so the sidecar keeps its watermark and
    re-sends the events instead of losing them."""
    from unittest.mock import patch

    body = json.dumps(
        {
            "provider": "sidecar-laptop",
            "sidecar_id": "laptop",
            "events": [
                {
                    "provider_id": "anthropic",
                    "account_id": "alice@example.com",
                    "event_id": "msg_1",
                    "ts": "2026-09-01T10:00:00Z",
                    "kind": "message",
                    "model_id": "sonnet-4.5",
                }
            ],
        }
    ).encode()
    ts, sig = _sign(body)
    with patch(
        "app.services.event_ingestor.EventIngestor.ingest", side_effect=RuntimeError("db locked")
    ):
        resp = client.post(
            "/api/v1/fleet/ingest",
            content=body,
            headers={"X-Timestamp": ts, "X-Signature": sig, "Content-Type": "application/json"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["events_error"] is True


def test_ingest_retires_the_plain_origin_row_a_keyed_origin_supersedes(
    client: TestClient, session: Session
):
    """zai's env origin used to be plain; once it is key-scoped the same credential reports
    under a new source id and the old row would sit there stale forever."""
    from app.services.account_identity import credential_fingerprint, keyed_credential_origin
    from app.services.credential_sources import sidecar_source_id, touch_source

    plain = "env:ZAI_API_KEY"
    touch_source(
        session,
        provider_id="zai",
        account_id="alice@example.com",
        source_id=sidecar_source_id("laptop", plain),
        source_type="env",
        source_label="ZAI_API_KEY",
        credential_origin=plain,
        sidecar_id="laptop",
    )
    # An unrelated machine's plain row must survive.
    touch_source(
        session,
        provider_id="zai",
        account_id="alice@example.com",
        source_id=sidecar_source_id("desktop", plain),
        source_type="env",
        source_label="ZAI_API_KEY",
        credential_origin=plain,
        sidecar_id="desktop",
    )
    session.commit()
    key = "zai-secret-key-value"  # pragma: allowlist secret
    keyed = keyed_credential_origin(plain, credential_fingerprint(key) or "")

    resp = _ingest(client, "laptop", [_token_card("zai", keyed, key, "alice@example.com")])

    assert resp.status_code == 200, resp.text
    ids = {r.source_id for r in session.exec(select(CredentialSource)).all()}
    assert sidecar_source_id("laptop", keyed) in ids
    assert sidecar_source_id("laptop", plain) not in ids, "superseded row is retired"
    assert sidecar_source_id("desktop", plain) in ids, "another machine's row is untouched"


def test_ingest_retires_earlier_fingerprints_of_a_rotating_xai_login(
    client: TestClient, session: Session
):
    """#523: xAI re-keys on every refresh, so a new ``base#<fp>`` means the old one is
    dead — its row, cached bundle and rotation tag must go, but nothing of another
    machine's, and never the operator's own tag."""
    from datetime import UTC, datetime, timedelta

    from app.models.db import CredentialTag
    from app.services.collector_manager import manager
    from app.services.credential_sources import sidecar_source_id, touch_source

    base = "path:/home/alice/.local/share/opencode/auth.json"
    old, older, new = (f"{base}#{fp}" for fp in ("aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"))
    for sidecar, origin in (("laptop", old), ("laptop", older), ("desktop", old)):
        row = touch_source(
            session,
            provider_id="xai",
            account_id="grok@example.com",
            source_id=sidecar_source_id(sidecar, origin),
            source_type="file",
            source_label="auth.json",
            credential_origin=origin,
            sidecar_id=sidecar,
        )
        if (sidecar, origin) == ("laptop", old):
            row.enabled = False
            row.priority = 4
        # ``old`` is the most recently seen of this machine's rows, so the successor inherits it.
        row.last_seen = datetime.now(UTC) - timedelta(days=0 if origin == old else 2)
        session.add(row)
    for origin, set_by in ((old, "operator"), (older, "rotation")):
        session.add(
            CredentialTag(
                provider_id="xai",
                credential_origin=origin,
                account_id="grok@example.com",
                sidecar_id="laptop",
                set_by=set_by,
            )
        )
    session.commit()

    resp = _ingest(client, "laptop", [_token_card("xai", new, "xai-token", "grok@example.com")])

    assert resp.status_code == 200, resp.text
    session.expire_all()
    rows = {r.source_id: r for r in session.exec(select(CredentialSource)).all()}
    assert set(rows) == {
        sidecar_source_id("laptop", new),
        sidecar_source_id("desktop", old),
    }, "earlier fingerprints on this machine are retired, another machine's is untouched"
    successor = rows[sidecar_source_id("laptop", new)]
    assert (successor.enabled, successor.priority) == (False, 4)
    prefs = manager.credential_source_preferences[("xai", "grok@example.com")]
    assert sidecar_source_id("laptop", old) not in prefs
    assert sidecar_source_id("laptop", older) not in prefs
    tags = {(t.set_by, t.credential_origin) for t in session.exec(select(CredentialTag)).all()}
    assert tags == {("operator", old)}, "rotation tags go, the operator's tag stays"


def test_xai_successor_inherits_from_the_newest_of_several_stale_rows(
    client: TestClient, session: Session
):
    """A deployment hit by #523 holds one stale row per past rotation; the operator's
    current enabled/priority live on the most recent, not on whichever the DB lists first."""
    from datetime import UTC, datetime, timedelta

    from app.services.credential_sources import sidecar_source_id, touch_source

    base = "path:/home/alice/.local/share/opencode/auth.json"
    now = datetime.now(UTC)
    # (fingerprint, age, enabled, priority) — the newest was disabled and ordered by the operator.
    for fp, age, enabled, priority in (
        ("aaaaaaaaaaaa", 3, True, 0),
        ("bbbbbbbbbbbb", 1, False, 9),
        ("dddddddddddd", 2, True, 0),
    ):
        origin = f"{base}#{fp}"
        row = touch_source(
            session,
            provider_id="xai",
            account_id="grok@example.com",
            source_id=sidecar_source_id("laptop", origin),
            source_type="file",
            source_label="auth.json",
            credential_origin=origin,
            sidecar_id="laptop",
        )
        row.enabled, row.priority, row.last_seen = enabled, priority, now - timedelta(days=age)
        session.add(row)
    session.commit()
    new = f"{base}#cccccccccccc"

    resp = _ingest(client, "laptop", [_token_card("xai", new, "xai-token", "grok@example.com")])

    assert resp.status_code == 200, resp.text
    session.expire_all()
    successor = session.exec(
        select(CredentialSource).where(
            CredentialSource.source_id == sidecar_source_id("laptop", new)
        )
    ).one()
    assert (successor.enabled, successor.priority) == (False, 9)


def test_ingest_keeps_other_fingerprints_for_a_provider_that_does_not_rekey(
    client: TestClient, session: Session
):
    from app.services.credential_sources import sidecar_source_id, touch_source

    base = "env:ZAI_API_KEY"
    other = f"{base}#aaaaaaaaaaaa"
    touch_source(
        session,
        provider_id="zai",
        account_id="alice@example.com",
        source_id=sidecar_source_id("laptop", other),
        source_type="env",
        source_label="ZAI_API_KEY",
        credential_origin=other,
        sidecar_id="laptop",
    )
    session.commit()
    new = f"{base}#bbbbbbbbbbbb"

    resp = _ingest(client, "laptop", [_token_card("zai", new, "k", "alice@example.com")])

    assert resp.status_code == 200, resp.text
    ids = {r.source_id for r in session.exec(select(CredentialSource)).all()}
    assert ids == {sidecar_source_id("laptop", other), sidecar_source_id("laptop", new)}


def test_ingest_leaves_a_plain_origin_row_alone_when_the_origin_stays_plain(
    client: TestClient, session: Session
):
    from app.services.credential_sources import sidecar_source_id

    resp = _ingest(
        client,
        "laptop",
        [_token_card("gemini", "path:/home/u/.gemini/oauth_creds.json", "k", "a@example.com")],
    )

    assert resp.status_code == 200, resp.text
    ids = {r.source_id for r in session.exec(select(CredentialSource)).all()}
    assert ids == {sidecar_source_id("laptop", "path:/home/u/.gemini/oauth_creds.json")}


def test_ingest_moves_a_source_off_a_phantom_account_it_is_filed_under_alone(
    client: TestClient, session: Session
):
    """The shape an out-of-band account rename leaves: the same source still filed
    under the id the account was renamed away from. Ingest re-points it, because
    nothing at all (config, card, usage, tag, label) stands behind that id."""
    from app.services.credential_sources import sidecar_source_id

    origin = "path:/home/u/.gemini/oauth_creds.json"
    source_id = sidecar_source_id("laptop", origin)
    _file_source(
        session,
        provider_id="gemini",
        account_id="hash:stale-identity",
        source_id=source_id,
        origin=origin,
    )
    _add_provider_config(session, provider_id="gemini", account_id="alice@example.com")
    session.commit()

    resp = _ingest(client, "laptop", [_token_card("gemini", origin, "k", "alice@example.com")])

    assert resp.status_code == 200, resp.text
    rows = session.exec(select(CredentialSource)).all()
    assert {(r.provider_id, r.account_id, r.source_id) for r in rows} == {
        ("gemini", "alice@example.com", source_id)
    }


def test_ingest_drops_a_phantom_copy_but_leaves_accounts_that_have_evidence(
    client: TestClient, session: Session
):
    """When the resolved account already holds the source, the stranded copy is deleted
    — and only that: a row under a second account with its own configuration is an
    operator choice, not ours to touch."""
    from app.services.credential_sources import sidecar_source_id

    origin = "path:/home/u/.gemini/oauth_creds.json"
    source_id = sidecar_source_id("laptop", origin)
    for account_id in ("hash:stale-identity", "alice@example.com", "bob@example.com"):
        _file_source(
            session,
            provider_id="gemini",
            account_id=account_id,
            source_id=source_id,
            origin=origin,
        )
    _add_provider_config(session, provider_id="gemini", account_id="alice@example.com")
    _add_provider_config(session, provider_id="gemini", account_id="bob@example.com")
    session.commit()

    resp = _ingest(client, "laptop", [_token_card("gemini", origin, "k", "alice@example.com")])

    assert resp.status_code == 200, resp.text
    rows = session.exec(select(CredentialSource)).all()
    assert {(r.account_id, r.source_id) for r in rows} == {
        ("alice@example.com", source_id),
        ("bob@example.com", source_id),
    }


def test_the_keyed_row_inherits_enabled_and_priority_from_the_row_it_replaces(
    client: TestClient, session: Session
):
    """An operator who disabled (or ordered) the plain source must not have that undone
    just because its origin became key-scoped."""
    from app.services.account_identity import credential_fingerprint, keyed_credential_origin
    from app.services.collector_manager import manager
    from app.services.credential_sources import sidecar_source_id, touch_source

    plain = "env:ZAI_API_KEY"
    old = touch_source(
        session,
        provider_id="zai",
        account_id="alice@example.com",
        source_id=sidecar_source_id("laptop", plain),
        source_type="env",
        source_label="ZAI_API_KEY",
        credential_origin=plain,
        sidecar_id="laptop",
    )
    old.enabled = False
    old.priority = 7
    session.add(old)
    session.commit()
    key = "zai-inherit-key-value"  # pragma: allowlist secret
    keyed = keyed_credential_origin(plain, credential_fingerprint(key) or "")

    resp = _ingest(client, "laptop", [_token_card("zai", keyed, key, "alice@example.com")])

    assert resp.status_code == 200, resp.text
    session.expire_all()
    new = session.exec(
        select(CredentialSource).where(
            CredentialSource.source_id == sidecar_source_id("laptop", keyed)
        )
    ).one()
    assert (new.enabled, new.priority) == (False, 7)
    prefs = manager.credential_source_preferences[("zai", "alice@example.com")]
    assert prefs[new.source_id] == (False, 7)
    assert old.source_id not in prefs


def test_manifest_carries_the_binding_across_a_fingerprint_rotation(
    client: TestClient, session: Session
):
    """A re-login re-keys the origin; the operator's binding must follow.

    Without the carry the new origin lands in ``pending_credential_tags``,
    the sidecar withholds the token for it (#349), and quota collection
    silently dies even though nothing about the credential's location or
    account changed.
    """
    from app.services.credential_tags import CredentialTagRepo, PendingCredentialTagRepo

    old, new = _rotation_origins()
    CredentialTagRepo.set_tag(
        session,
        provider_id="opencode",
        credential_origin=old,
        account_id="alice@example.com",
        sidecar_id="alpha-host",
    )
    session.commit()

    r = _post_manifest(
        client,
        {
            "sidecar_id": "alpha-host",
            "completed_providers": ["opencode"],
            "entries": [{"provider_id": "opencode", "credential_origin": new}],
        },
    )
    assert r.status_code == 200, r.text

    tag = CredentialTagRepo.get(
        session, provider_id="opencode", credential_origin=new, sidecar_id="alpha-host"
    )
    assert tag is not None
    assert tag.account_id == "alice@example.com"
    assert tag.set_by == "rotation"
    assert PendingCredentialTagRepo.list_all(session, sidecar_id="alpha-host") == []
    # The new origin ships as a hint on this same round-trip, so the
    # sidecar's next push already carries the token.
    assert r.json()["resolved"]["opencode"][new] == "alice@example.com"


def test_manifest_keeps_a_rotation_pending_when_two_accounts_shared_the_location(
    client: TestClient, session: Session
):
    """Two accounts ever tagged here means it is the operator's call, not ours."""
    from app.services.credential_tags import CredentialTagRepo, PendingCredentialTagRepo

    base = "path:/home/shared/.local/share/opencode/auth.json"
    CredentialTagRepo.set_tag(
        session,
        provider_id="opencode",
        credential_origin=f"{base}#0123456789ab",
        account_id="alice@example.com",
        sidecar_id="alpha-host",
    )
    CredentialTagRepo.set_tag(
        session,
        provider_id="opencode",
        credential_origin=f"{base}#000000000000",
        account_id="bob@example.com",
        sidecar_id=None,
    )
    session.commit()

    new = f"{base}#abcdef012345"
    r = _post_manifest(
        client,
        {
            "sidecar_id": "alpha-host",
            "completed_providers": ["opencode"],
            "entries": [{"provider_id": "opencode", "credential_origin": new}],
        },
    )
    assert r.status_code == 200, r.text

    assert (
        CredentialTagRepo.get(
            session, provider_id="opencode", credential_origin=new, sidecar_id="alpha-host"
        )
        is None
    )
    pending = PendingCredentialTagRepo.list_all(session, sidecar_id="alpha-host")
    assert [row.credential_origin for row in pending] == [new]
    assert new not in r.json()["resolved"].get("opencode", {})


def test_manifest_does_not_inherit_another_machines_binding(client: TestClient, session: Session):
    from app.services.credential_tags import CredentialTagRepo, PendingCredentialTagRepo

    old, new = _rotation_origins()
    CredentialTagRepo.set_tag(
        session,
        provider_id="opencode",
        credential_origin=old,
        account_id="alice@example.com",
        sidecar_id="beta-host",
    )
    session.commit()

    r = _post_manifest(
        client,
        {
            "sidecar_id": "alpha-host",
            "completed_providers": ["opencode"],
            "entries": [{"provider_id": "opencode", "credential_origin": new}],
        },
    )
    assert r.status_code == 200, r.text

    assert (
        CredentialTagRepo.get(
            session, provider_id="opencode", credential_origin=new, sidecar_id="alpha-host"
        )
        is None
    )
    pending = PendingCredentialTagRepo.list_all(session, sidecar_id="alpha-host")
    assert [row.credential_origin for row in pending] == [new]


def test_manifest_rotation_yields_to_a_contradicting_reported_identity(
    client: TestClient, session: Session
):
    """If the credential now claims a different account, the old binding must not win."""
    from app.services.credential_tags import CredentialTagRepo, PendingCredentialTagRepo

    old, new = _rotation_origins()
    CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin=old,
        account_id="alice@example.com",
        sidecar_id="alpha-host",
    )
    session.commit()

    r = _post_manifest(
        client,
        {
            "sidecar_id": "alpha-host",
            "completed_providers": ["anthropic"],
            "entries": [
                {
                    "provider_id": "anthropic",
                    "credential_origin": new,
                    "account_id": "bob@example.com",
                }
            ],
        },
    )
    assert r.status_code == 200, r.text

    assert (
        CredentialTagRepo.get(
            session, provider_id="anthropic", credential_origin=new, sidecar_id="alpha-host"
        )
        is None
    )
    pending = PendingCredentialTagRepo.list_all(session, sidecar_id="alpha-host")
    assert [row.credential_origin for row in pending] == [new]
    assert pending[0].claimed_account_id == "bob@example.com"


@pytest.mark.parametrize(
    ("provider", "token_key", "device", "expected"),
    [
        ("kimi_coding", "cli_access_token", " cli-device\n", "cli-device"),
        ("kimi_coding", "cli_access_token", "bad\x00device", None),
        ("kimi_coding", "api_key", "cli-device", None),
        ("kimi_api", "cli_access_token", "cli-device", None),
    ],
)
@pytest.mark.asyncio
async def test_ingest_accepts_device_only_in_kimi_cli_bundle(
    client, monkeypatch, provider, token_key, device, expected
):
    from app.services.token_cache import TokenCache

    cache = TokenCache()
    monkeypatch.setattr("app.api.endpoints.fleet_ingest.token_cache", cache)
    payload = {
        "provider": "sidecar",
        "sidecar_id": "device-host",
        "metrics": [
            {
                "service_name": "Kimi",
                "remaining": "Token",
                "unit": "oauth",
                "account_id": "work",
                "metadata": {
                    "provider_id": provider,
                    token_key: "test-bearer",
                    "cli_expires_at": str(time.time() + 3600),
                    "cli_device_id": device,
                },
            }
        ],
    }
    body = json.dumps(payload).encode()
    ts, sig = _sign(body)
    response = client.post(
        "/api/v1/fleet/ingest",
        content=body,
        headers={"X-Timestamp": ts, "X-Signature": sig, "Content-Type": "application/json"},
    )
    assert response.status_code == 200, response.text
    bundles = await cache.get_source_candidates(provider, "work")
    assert len(bundles) == 1
    assert bundles[0]["tokens"].get("cli_device_id") == expected
    assert bundles[0]["tokens"][token_key] == "test-bearer"


async def test_ingest_keeps_the_reported_agy_version(client: TestClient, session: Session):
    """The agy version the sidecar reported with the token survives ingest (#687).

    The Code Assist API gates *which quota pools* it serves on the User-Agent's
    version, so the collector has to be able to read the real one. A sidecar
    that reports no version must still push its token.
    """
    from app.services.token_cache import token_cache

    card = {
        "service_name": "Antigravity",
        "remaining": "Token",
        "unit": "oauth",
        "account_id": "user@example.com",
        "metadata": {
            "provider_id": "antigravity",
            "credential_origin": "path:/home/u/.gemini/antigravity-cli/antigravity-oauth-token",
            "oauth_token": "ya29.live",
            "agy_version": "1.5.2",
        },
    }
    response = _ingest(client, "native-host", [card])
    assert response.status_code == 200, response.text

    tokens, _metadata = await token_cache.get_with_metadata(
        "antigravity", account_id="user@example.com"
    )
    assert tokens is not None
    assert tokens.get("agy_version") == "1.5.2"
    # The token itself must survive untouched.
    assert tokens.get("oauth_token") == "ya29.live"
