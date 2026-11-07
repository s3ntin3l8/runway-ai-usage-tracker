"""Integration tests for parked/retired accounts, unavailable sources, and archived redirects."""

from __future__ import annotations

import pytest
from sqlmodel import Session, select

from app.models.db import CredentialSource, ProviderConfig
from tests.fixtures.test_client import TestClient
from tests.integration._fleet_credentials_helpers import (
    _add_archived_provider_config,
    _add_provider_config,
    _ingest,
    _isolated_ingest_key,  # noqa: F401 — fixture
    _post_manifest,
    _token_card,
    client_fixture,  # noqa: F401 — fixture
    session_fixture,  # noqa: F401 — fixture
)

# --- Gemini usage -> archived Gemini / Antigravity targets ------------------


def _pending_gemini(session: Session, event_id: str, sidecar_id: str = "laptop") -> int:
    from datetime import datetime

    from app.models.db import PendingUsageEvent
    from app.models.schemas import UsageEventPush

    push = UsageEventPush(
        provider_id="gemini",
        account_id="default",
        account_source="default",
        event_id=event_id,
        ts="2026-09-01T10:00:00Z",
        model_id="gemini-3-pro-preview",
    )
    row = PendingUsageEvent(
        provider_id="gemini",
        event_id=event_id,
        sidecar_id=sidecar_id,
        ts=datetime.fromisoformat(push.ts.replace("Z", "+00:00")),
        payload_json=push.model_dump_json(),
    )
    session.add(row)
    session.commit()
    assert row.id is not None
    return row.id


def _known_antigravity(monkeypatch: pytest.MonkeyPatch, account_id: str = "me@example.com"):
    async def _accounts():
        return [("antigravity", account_id, account_id)]

    # Same instance-level patch as the neighbouring tests: earlier ones leave a
    # bound-method instance attribute behind that would shadow a class patch.
    monkeypatch.setattr(
        "app.api.endpoints.fleet_pending_events.token_cache.get_all_active_accounts", _accounts
    )


# A credential source the sidecar found but could not read (#711):
#   POST /fleet/credentials/manifest  — sidecar reports a locked cookie store
#   GET  /system/credentials           — the inventory lists it
#   GET  /usage/fleet                  — the provider page reads it
# ---------------------------------------------------------------------------


def _blocked_entry(provider_id="ollama", reason="source_locked", origin=None):
    return {
        "provider_id": provider_id,
        "credential_origin": origin or f"cookie:{provider_id}/Chrome/Default/Network/Cookies",
        "reason": reason,
        "source_store": "Chrome/Default/Network/Cookies",
        "source_detail": "held open by another process",
        "source_remedy": "set OLLAMA_SESSION_TOKEN (or paste the cookie in Settings → Providers)",
    }


def _admin_headers() -> dict[str, str]:
    from app.core.config import settings

    return {"X-Admin-Key": settings.ADMIN_API_KEY} if settings.ADMIN_API_KEY else {}


@pytest.fixture(name="inventory_session")
def inventory_session_fixture(session: Session, monkeypatch):
    """Point the credential inventory at the test session.

    The inventory opens its own session on the app engine, so a test that
    reads it needs the test engine instead.
    """
    from app.services.credentials import inventory as credential_inventory

    monkeypatch.setattr(credential_inventory, "engine", session.get_bind())
    monkeypatch.setattr(credential_inventory, "_scan_server_credentials", lambda: ({}, set()))
    return session


def test_gemini_event_assigns_to_antigravity_and_redirects_future_events(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
):
    from app.models.db import CredentialTag, PendingUsageEvent, UsageEvent
    from app.models.schemas import UsageEventPush
    from app.services.event_ingestor import EventIngestor

    _known_antigravity(monkeypatch)
    event_pk = _pending_gemini(session, "g-1")

    response = client.post(
        "/api/v1/fleet/events/pending/assign",
        json={
            "event_ids": [event_pk],
            "account_id": "me@example.com",
            "target_provider_id": "antigravity",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["target_provider_id"] == "antigravity"
    stored = session.exec(select(UsageEvent)).one()
    assert (stored.provider_id, stored.account_id) == ("antigravity", "me@example.com")
    assert session.exec(select(PendingUsageEvent)).all() == []
    tag = session.exec(select(CredentialTag)).one()
    assert (tag.provider_id, tag.sidecar_id, tag.target_provider_id) == (
        "gemini",
        "laptop",
        "antigravity",
    )

    def push(event_id: str, source: str, account: str) -> UsageEventPush:
        return UsageEventPush(
            provider_id="gemini",
            account_id=account,
            account_source=source,
            event_id=event_id,
            ts="2026-09-01T11:00:00Z",
            model_id="gemini-3-pro-preview",
        )

    EventIngestor(session).ingest(
        [
            push("g-default", "default", "default"),
            push("g-tag", "tag", "me@example.com"),
            push("g-other", "tag", "someone-else@example.com"),
        ],
        sidecar_id="laptop",
    )
    by_event = {e.event_id: (e.provider_id, e.account_id) for e in session.exec(select(UsageEvent))}
    assert by_event["g-default"] == ("antigravity", "me@example.com")
    assert by_event["g-tag"] == ("antigravity", "me@example.com")
    assert by_event["g-other"] == ("gemini", "someone-else@example.com")

    EventIngestor(session).ingest([push("g-elsewhere", "default", "default")], sidecar_id="desktop")
    pending = session.exec(select(PendingUsageEvent)).one()
    assert (pending.event_id, pending.sidecar_id) == ("g-elsewhere", "desktop")


def test_redirect_does_not_double_count_event_already_stored_under_gemini(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
):
    from app.models.db import UsageEvent
    from app.models.schemas import UsageEventPush
    from app.services.event_ingestor import EventIngestor

    _known_antigravity(monkeypatch)
    EventIngestor(session).ingest(
        [
            UsageEventPush(
                provider_id="gemini",
                account_id="old@example.com",
                account_source="tag",
                event_id="g-old",
                ts="2026-08-01T10:00:00Z",
                model_id="m",
            )
        ],
        sidecar_id="laptop",
    )
    event_pk = _pending_gemini(session, "g-1")
    client.post(
        "/api/v1/fleet/events/pending/assign",
        json={
            "event_ids": [event_pk],
            "account_id": "me@example.com",
            "target_provider_id": "antigravity",
        },
    )

    EventIngestor(session).ingest(
        [
            UsageEventPush(
                provider_id="gemini",
                account_id="default",
                account_source="default",
                event_id="g-old",
                ts="2026-08-01T10:00:00Z",
                model_id="m",
            )
        ],
        sidecar_id="laptop",
    )
    stored = [e for e in session.exec(select(UsageEvent)) if e.event_id == "g-old"]
    assert [(e.provider_id, e.account_id) for e in stored] == [("gemini", "old@example.com")]


def test_pending_assign_rejects_target_provider_outside_allow_list(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
):
    _known_antigravity(monkeypatch)
    event_pk = _pending_gemini(session, "g-1")

    response = client.post(
        "/api/v1/fleet/events/pending/assign",
        json={
            "event_ids": [event_pk],
            "account_id": "me@example.com",
            "target_provider_id": "anthropic",
        },
    )

    assert response.status_code == 422, response.text


def test_pending_assign_accepts_archived_account_but_not_disabled(
    client: TestClient, session: Session
):
    from app.models.db import UsageEvent

    _add_provider_config(session, provider_id="gemini", account_id="old@example.com")
    cfg = session.exec(
        select(ProviderConfig).where(ProviderConfig.account_id == "old@example.com")
    ).one()
    cfg.archived = True
    cfg.enabled = False
    session.add(cfg)
    session.commit()
    event_pk = _pending_gemini(session, "g-1")

    response = client.post(
        "/api/v1/fleet/events/pending/assign",
        json={"event_ids": [event_pk], "account_id": "old@example.com"},
    )

    assert response.status_code == 200, response.text
    stored = session.exec(select(UsageEvent)).one()
    assert (stored.provider_id, stored.account_id) == ("gemini", "old@example.com")

    cfg.archived = False
    session.add(cfg)
    session.commit()
    event_pk = _pending_gemini(session, "g-2")
    response = client.post(
        "/api/v1/fleet/events/pending/assign",
        json={"event_ids": [event_pk], "account_id": "old@example.com"},
    )
    assert response.status_code == 404, response.text


def test_batch_rejects_conflicting_targets_for_same_provider_and_host(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
):
    from app.models.db import PendingUsageEvent

    _known_antigravity(monkeypatch)
    _add_provider_config(session, provider_id="gemini", account_id="me@example.com")
    first = _pending_gemini(session, "g-1")
    second = _pending_gemini(session, "g-2")

    response = client.post(
        "/api/v1/fleet/events/pending/assign-batch",
        json={
            "assignments": [
                {
                    "event_ids": [first],
                    "account_id": "me@example.com",
                    "target_provider_id": "antigravity",
                },
                {"event_ids": [second], "account_id": "me@example.com"},
            ]
        },
    )

    assert response.status_code == 422, response.text
    assert len(session.exec(select(PendingUsageEvent)).all()) == 2


def test_reassigning_to_gemini_clears_redirect(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
):
    from app.models.db import CredentialTag

    _known_antigravity(monkeypatch)
    _add_provider_config(session, provider_id="gemini", account_id="me@example.com")
    first = _pending_gemini(session, "g-1")
    initial = client.post(
        "/api/v1/fleet/events/pending/assign",
        json={
            "event_ids": [first],
            "account_id": "me@example.com",
            "target_provider_id": "antigravity",
        },
    )
    assert initial.status_code == 200, initial.text
    second = _pending_gemini(session, "g-2")

    response = client.post(
        "/api/v1/fleet/events/pending/assign",
        json={"event_ids": [second], "account_id": "me@example.com"},
    )

    assert response.status_code == 200, response.text
    session.expire_all()
    assert session.exec(select(CredentialTag)).one().target_provider_id is None


def test_delete_by_account_matches_effective_provider(session: Session):
    from app.models.db import CredentialTag
    from app.services.credential_tags import CredentialTagRepo

    CredentialTagRepo.set_tag(
        session,
        provider_id="gemini",
        credential_origin="provider:gemini",
        account_id="me@example.com",
        sidecar_id="laptop",
        target_provider_id="antigravity",
    )
    session.commit()

    assert (
        CredentialTagRepo.delete_by_account(
            session, provider_id="gemini", account_id="me@example.com"
        )
        == 0
    )
    assert (
        CredentialTagRepo.delete_by_account(
            session, provider_id="antigravity", account_id="me@example.com"
        )
        == 1
    )
    assert session.exec(select(CredentialTag)).all() == []


def test_mixed_provider_assignment_keeps_each_events_own_provider(
    client: TestClient, session: Session
):
    from datetime import datetime

    from app.models.db import CredentialTag, PendingUsageEvent, UsageEvent
    from app.models.schemas import UsageEventPush

    _add_provider_config(session, provider_id="gemini", account_id="me@example.com")
    _add_provider_config(session, provider_id="xai", account_id="me@example.com")
    gemini_pk = _pending_gemini(session, "g-1")
    push = UsageEventPush(
        provider_id="xai",
        account_id="default",
        account_source="default",
        event_id="x-1",
        ts="2026-09-01T10:00:00Z",
        model_id="grok",
    )
    xai_row = PendingUsageEvent(
        provider_id="xai",
        event_id="x-1",
        sidecar_id="laptop",
        ts=datetime.fromisoformat(push.ts.replace("Z", "+00:00")),
        payload_json=push.model_dump_json(),
    )
    session.add(xai_row)
    session.commit()

    response = client.post(
        "/api/v1/fleet/events/pending/assign",
        json={"event_ids": [gemini_pk, xai_row.id], "account_id": "me@example.com"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["assigned"] == 2
    # Back-compat fields describe the first group; ``mappings`` covers every group.
    assert body["provider_id"] in {"gemini", "xai"}
    assert {
        (m["provider_id"], m["target_provider_id"], m["account_id"], m["assigned"])
        for m in body["mappings"]
    } == {
        ("gemini", "gemini", "me@example.com", 1),
        ("xai", "xai", "me@example.com", 1),
    }
    stored = {(e.provider_id, e.account_id) for e in session.exec(select(UsageEvent))}
    assert stored == {("gemini", "me@example.com"), ("xai", "me@example.com")}
    assert all(t.target_provider_id is None for t in session.exec(select(CredentialTag)))

    # An explicit related-provider target is rejected when any event isn't allowed it.
    gemini_pk = _pending_gemini(session, "g-2")
    xai_row = PendingUsageEvent(
        provider_id="xai",
        event_id="x-2",
        sidecar_id="laptop",
        ts=datetime.fromisoformat(push.ts.replace("Z", "+00:00")),
        payload_json=push.model_copy(update={"event_id": "x-2"}).model_dump_json(),
    )
    session.add(xai_row)
    session.commit()
    rejected = client.post(
        "/api/v1/fleet/events/pending/assign",
        json={
            "event_ids": [gemini_pk, xai_row.id],
            "account_id": "me@example.com",
            "target_provider_id": "antigravity",
        },
    )
    assert rejected.status_code == 422, rejected.text


def test_tag_endpoint_parks_a_credential_on_an_archived_account(
    client: TestClient, session: Session
):
    """Tagging an archived account is accepted and reported as parked."""
    from app.services.credential_tags import PendingCredentialTagRepo

    _add_archived_provider_config(
        session, provider_id="kimi_coding", account_id="s3ntin318@gmail.com"
    )
    PendingCredentialTagRepo.upsert(
        session,
        sidecar_id="alpha-host",
        provider_id="kimi_coding",
        credential_origin="path:/home/u/.kimi/credentials.json",
    )
    session.commit()

    resp = client.post(
        "/api/v1/fleet/credentials/tags",
        json={
            "sidecar_id": "alpha-host",
            "provider_id": "kimi_coding",
            "credential_origin": "path:/home/u/.kimi/credentials.json",
            "account_id": "s3ntin318@gmail.com",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["target_archived"] is True

    # The origin is resolved: the pending row is gone, so the operator is not
    # nagged about a credential they already dealt with.
    assert PendingCredentialTagRepo.list_all(session) == []


def test_tag_endpoint_reports_a_live_target_as_not_parked(client: TestClient, session: Session):
    """A normal mapping is not parked — the flag tells the two apart."""
    _add_provider_config(
        session, provider_id="anthropic", account_id="alice@example.com", api_key="sk-test"
    )

    resp = client.post(
        "/api/v1/fleet/credentials/tags",
        json={
            "sidecar_id": "alpha-host",
            "provider_id": "anthropic",
            "credential_origin": "path:/.claude/.credentials.json",
            "account_id": "alice@example.com",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["target_archived"] is False


def test_parked_mapping_keeps_the_credential_where_the_machine_reported_it(
    client: TestClient, session: Session
):
    """Parking does not re-file the credential under the retired account.

    The server stops honouring the parked mapping, so a later push would file
    the source under the machine's own resolution again — moving it now would
    leave two rows for one source.
    """
    from app.services.credential_sources import sidecar_source_id, touch_source

    origin = "path:/home/u/.kimi/credentials.json"
    source_id = sidecar_source_id("alpha-host", origin)
    touch_source(
        session,
        provider_id="kimi_coding",
        account_id="default",
        source_id=source_id,
        source_type="path",
        source_label=".kimi/credentials.json",
        credential_origin=origin,
        sidecar_id="alpha-host",
    )
    _add_archived_provider_config(
        session, provider_id="kimi_coding", account_id="s3ntin318@gmail.com"
    )
    session.commit()

    resp = client.post(
        "/api/v1/fleet/credentials/tags",
        json={
            "sidecar_id": "alpha-host",
            "provider_id": "kimi_coding",
            "credential_origin": origin,
            "account_id": "s3ntin318@gmail.com",
        },
    )
    assert resp.status_code == 200, resp.text

    rows = session.exec(
        select(CredentialSource).where(CredentialSource.credential_origin == origin)
    ).all()
    assert [(row.account_id, row.source_id) for row in rows] == [("default", source_id)]


def test_parked_mapping_ships_no_hint_to_the_sidecar(client: TestClient, session: Session):
    """A parked mapping must never reach the sidecar.

    Shipping it would make the sidecar stamp the retired account with new
    usage — resurrecting a pair the operator closed. The live mapping in the
    same response is the control: it does ship.
    """
    from app.services.credential_tags import CredentialTagRepo

    _add_archived_provider_config(
        session, provider_id="kimi_coding", account_id="s3ntin318@gmail.com"
    )
    _add_provider_config(
        session, provider_id="anthropic", account_id="alice@example.com", api_key="sk-test"
    )
    CredentialTagRepo.set_tag(
        session,
        provider_id="kimi_coding",
        credential_origin="path:/home/u/.kimi/credentials.json",
        account_id="s3ntin318@gmail.com",
    )
    CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="path:/.claude/.credentials.json",
        account_id="alice@example.com",
    )
    session.commit()

    r = client.get("/api/v1/fleet/config")
    assert r.status_code == 200
    hints = r.json()["account_tag_hints"]
    assert hints.get("kimi_coding", {}) == {}
    # The live mapping is untouched by the parked filter.
    assert hints["anthropic"] == {"path:/.claude/.credentials.json": "alice@example.com"}


def test_restoring_an_archived_account_revives_the_parked_mapping(
    client: TestClient, session: Session
):
    """Parked-ness is derived, not stored: restoring the account ships the
    hint again — no re-tagging needed."""
    from app.services.credential_tags import CredentialTagRepo

    row = _add_archived_provider_config(
        session, provider_id="kimi_coding", account_id="s3ntin318@gmail.com"
    )
    CredentialTagRepo.set_tag(
        session,
        provider_id="kimi_coding",
        credential_origin="path:/home/u/.kimi/credentials.json",
        account_id="s3ntin318@gmail.com",
    )
    session.commit()

    assert (
        client.get("/api/v1/fleet/config").json()["account_tag_hints"].get("kimi_coding", {}) == {}
    )

    row.archived = False
    row.enabled = True
    session.add(row)
    session.commit()

    hints = client.get("/api/v1/fleet/config").json()["account_tag_hints"]
    assert hints["kimi_coding"] == {"path:/home/u/.kimi/credentials.json": "s3ntin318@gmail.com"}


def test_ingest_does_not_stamp_a_parked_account(client: TestClient, session: Session):
    """The ingest path resolves a pushed credential through the tag. A parked
    tag must not hand the retired account to the incoming card."""
    from app.services.credential_tags import CredentialTagRepo

    _add_archived_provider_config(
        session, provider_id="kimi_coding", account_id="s3ntin318@gmail.com"
    )
    CredentialTagRepo.set_tag(
        session,
        provider_id="kimi_coding",
        credential_origin="path:/home/u/.kimi/credentials.json",
        account_id="s3ntin318@gmail.com",
        sidecar_id="alpha-host",
    )
    session.commit()

    card = _token_card(
        "kimi_coding",
        "path:/home/u/.kimi/credentials.json",
        "sk-parked",
    )
    response = _ingest(client, "alpha-host", [card])
    assert response.status_code == 200, response.text

    rows = session.exec(
        select(CredentialSource).where(
            CredentialSource.provider_id == "kimi_coding",
            CredentialSource.credential_origin == "path:/home/u/.kimi/credentials.json",
        )
    ).all()
    assert rows, "the ingest must still record the credential it received"
    assert [row.account_id for row in rows] != ["s3ntin318@gmail.com"]


def test_manifest_does_not_reopen_a_parked_origin(client: TestClient, session: Session):
    """A parked origin stays resolved: the manifest must not file it as
    pending again, and must not report it as resolved to the sidecar."""
    from app.services.credential_tags import CredentialTagRepo, PendingCredentialTagRepo

    _add_archived_provider_config(
        session, provider_id="kimi_coding", account_id="s3ntin318@gmail.com"
    )
    CredentialTagRepo.set_tag(
        session,
        provider_id="kimi_coding",
        credential_origin="path:/home/u/.kimi/credentials.json",
        account_id="s3ntin318@gmail.com",
        sidecar_id="alpha-host",
    )
    session.commit()

    body = {
        "sidecar_id": "alpha-host",
        "completed_providers": ["kimi_coding"],
        "entries": [
            {
                "provider_id": "kimi_coding",
                "credential_origin": "path:/home/u/.kimi/credentials.json",
            }
        ],
    }
    result = _post_manifest(client, body)
    assert result.status_code == 200, result.text
    # Nothing to resolve for the sidecar: a parked mapping is not shipped.
    assert result.json()["resolved"].get("kimi_coding", {}) == {}
    # And it does not come back as pending.
    assert PendingCredentialTagRepo.list_all(session, sidecar_id="alpha-host") == []


def test_parked_redirect_ships_no_hint_and_does_not_file_the_card(
    client: TestClient, session: Session
):
    """A redirect tag names an account of its *target* provider.

    The parked-ness of that account has to be looked up under the target
    provider — keying the check by the tag's own provider would miss exactly
    this case, and the retired account would keep receiving the redirected
    credential.
    """
    from app.services.credential_tags import CredentialTagRepo

    _add_archived_provider_config(
        session, provider_id="antigravity", account_id="retired@example.com"
    )
    # A deployment-wide tag: /fleet/config only ships deployment-wide rows when
    # no sidecar is live, so a machine-scoped tag would make the hint
    # assertion below vacuous.
    CredentialTagRepo.set_tag(
        session,
        provider_id="gemini",
        credential_origin="provider:gemini",
        account_id="retired@example.com",
        target_provider_id="antigravity",
    )
    # The control: a live deployment-wide mapping in the same response, so
    # "no hint for gemini" is evidence rather than an empty map.
    CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="provider:anthropic",
        account_id="live@example.com",
    )
    session.commit()

    # No hint ships for the source provider: the sidecar must not learn that
    # the credential belongs to the retired account.
    hints = client.get("/api/v1/fleet/config").json()["account_tag_hints"]
    assert hints.get("gemini", {}) == {}
    assert hints["anthropic"] == {"provider:anthropic": "live@example.com"}

    response = _ingest(
        client,
        "alpha-host",
        [_token_card("gemini", "provider:gemini", "sk-redirected")],
    )
    assert response.status_code == 200, response.text

    rows = session.exec(
        select(CredentialSource).where(CredentialSource.provider_id == "gemini")
    ).all()
    assert rows, "the ingest must still record the credential it received"
    assert [row.account_id for row in rows] != ["retired@example.com"]


# ---------------------------------------------------------------------------
# A retired account holds no credential (#895)
#
# The machine still has the credential file and keeps pushing it, so the
# ingest resolves the credential to the retired account again. The source
# record stays (the operator still sees the machine reporting it, under the
# archived account) — but the secret is not kept: not in the cache, not at
# rest. A retired account is not a place a live credential can live.
# ---------------------------------------------------------------------------


async def test_ingest_does_not_store_the_secret_of_a_retired_account(
    client: TestClient, session: Session
):
    """The record stays; the secret does not.

    The eviction of the archive write path (#891) was undone by the machine's
    next push: the credential was filed under the retired account again. The
    source record must still be filed there — the operator needs to see that
    a machine reports a credential for the account they retired — but nothing
    live may be held for it.
    """
    from app.services.credential_sources import sidecar_source_id
    from app.services.token_cache import token_cache

    account_id = "retired@example.test"
    origin = "path:/home/u/.kimi/credentials.json"
    _add_archived_provider_config(session, provider_id="kimi_coding", account_id=account_id)

    response = _ingest(
        client,
        "alpha-host",
        [_token_card("kimi_coding", origin, "sk-retired", account_id=account_id)],
    )
    assert response.status_code == 200, response.text

    # The record stays under the retired account.
    rows = session.exec(
        select(CredentialSource).where(
            CredentialSource.provider_id == "kimi_coding",
            CredentialSource.credential_origin == origin,
        )
    ).all()
    assert [(row.account_id, row.source_id) for row in rows] == [
        (account_id, sidecar_source_id("alpha-host", origin))
    ]

    # But no secret is held for it — neither in the cache nor at rest.
    assert (
        await token_cache.get_with_metadata("kimi_coding", account_id=account_id, exact=True)
        is None
    )
    assert account_id not in token_cache._source_cache.get("kimi_coding", {})


async def test_restoring_a_retired_account_lets_the_next_push_store_the_credential_again(
    client: TestClient, session: Session
):
    """Retiring is reversible: the machine still has the credential file.

    Once the account is live again, the next push files the credential under
    it — nothing else has to be done by the operator.
    """
    from app.services.token_cache import token_cache

    account_id = "restored@example.test"
    origin = "path:/home/u/.kimi/credentials.json"
    row = _add_provider_config(
        session, provider_id="kimi_coding", account_id=account_id, enabled=True
    )
    row.archived = True
    row.enabled = False
    session.add(row)
    session.commit()

    card = _token_card("kimi_coding", origin, "sk-restored", account_id=account_id)
    response = _ingest(client, "alpha-host", [card])
    assert response.status_code == 200, response.text
    assert (
        await token_cache.get_with_metadata("kimi_coding", account_id=account_id, exact=True)
        is None
    )

    row.archived = False
    row.enabled = True
    session.add(row)
    session.commit()

    # A different body: the ingest rejects a replayed one.
    response = _ingest(
        client,
        "alpha-host",
        [_token_card("kimi_coding", origin, "sk-restored-again", account_id=account_id)],
    )
    assert response.status_code == 200, response.text

    tokens, _metadata = await token_cache.get_with_metadata(
        "kimi_coding", account_id=account_id, exact=True
    )
    assert tokens is not None
    assert tokens.get("api_key") == "sk-restored-again"


def test_a_locked_cookie_store_is_recorded_as_an_unavailable_source(client, session):
    from app.services.credential_tags import PendingCredentialTagRepo

    body = {
        "sidecar_id": "alpha-host",
        "completed_providers": ["ollama"],
        "entries": [_blocked_entry()],
    }
    result = _post_manifest(client, body)
    assert result.status_code == 200, result.text

    pending = PendingCredentialTagRepo.list_all(session, sidecar_id="alpha-host")
    assert len(pending) == 1
    row = pending[0]
    assert row.reason == "source_locked"
    assert row.source_store == "Chrome/Default/Network/Cookies"
    assert row.source_detail == "held open by another process"
    assert row.source_remedy.startswith("set OLLAMA_SESSION_TOKEN")


def test_an_unavailable_source_is_not_an_untagged_credential(client, session, inventory_session):
    """There is no credential to map, so it must not appear in the Untagged list."""
    body = {
        "sidecar_id": "alpha-host",
        "completed_providers": ["ollama"],
        "entries": [_blocked_entry()],
    }
    assert _post_manifest(client, body).status_code == 200

    listed = client.get("/api/v1/fleet/credentials/tags/pending")
    assert listed.status_code == 200
    assert listed.json()["items"] == []

    inventory = client.get("/api/v1/system/credentials", headers=_admin_headers())
    assert inventory.status_code == 200, inventory.text
    body_json = inventory.json()
    assert body_json["unmapped_count"] == 0
    assert [source["unmapped_count"] for source in body_json["machines"]] == [0]


def test_an_unavailable_source_is_listed_with_its_store_and_remedy(client, inventory_session):
    body = {
        "sidecar_id": "alpha-host",
        "completed_providers": ["ollama"],
        "entries": [_blocked_entry()],
    }
    assert _post_manifest(client, body).status_code == 200

    inventory = client.get("/api/v1/system/credentials", headers=_admin_headers())
    assert inventory.status_code == 200, inventory.text
    (blocked,) = inventory.json()["blocked_sources"]
    assert blocked["provider_id"] == "ollama"
    assert blocked["sidecar_id"] == "alpha-host"
    assert blocked["credential_origin"] == "cookie:ollama/Chrome/Default/Network/Cookies"
    assert blocked["source_store"] == "Chrome/Default/Network/Cookies"
    assert blocked["reason"] == "source_locked"
    assert blocked["detail"] == "held open by another process"
    assert blocked["remedy"].startswith("set OLLAMA_SESSION_TOKEN")
    assert blocked["collecting"] is False


def test_the_fleet_view_carries_the_unavailable_sources(client):
    body = {
        "sidecar_id": "alpha-host",
        "completed_providers": ["ollama"],
        "entries": [_blocked_entry()],
    }
    assert _post_manifest(client, body).status_code == 200

    fleet = client.get("/api/v1/usage/fleet")
    assert fleet.status_code == 200, fleet.text
    (blocked,) = fleet.json()["blocked_sources"]
    assert blocked["provider_id"] == "ollama"
    assert blocked["source_store"] == "Chrome/Default/Network/Cookies"


def test_an_unavailable_source_disappears_when_the_store_becomes_readable(
    client, session, inventory_session
):
    """The report is pruned the cycle the sidecar stops sending it."""
    blocked_body = {
        "sidecar_id": "alpha-host",
        "completed_providers": ["ollama"],
        "entries": [_blocked_entry()],
    }
    assert _post_manifest(client, blocked_body).status_code == 200

    readable_body = {
        "sidecar_id": "alpha-host",
        "completed_providers": ["ollama"],
        "entries": [],
    }
    assert _post_manifest(client, readable_body).status_code == 200

    inventory = client.get("/api/v1/system/credentials", headers=_admin_headers())
    assert inventory.status_code == 200, inventory.text
    assert inventory.json()["blocked_sources"] == []


def test_an_unavailable_source_disappears_when_the_provider_is_withheld(
    client, session, inventory_session
):
    """The report is retired even when the provider is not reported as collected.

    On macOS the sidecar withholds a provider with a native source from
    ``completed_providers`` - a withheld scan must never read as a logout - so
    the row cannot wait for the provider to be collected. Its lifecycle is the
    report itself: the sidecar repeats it every cycle while the store stays
    unreadable, and stops when the store reads fine.
    """
    blocked_body = {
        "sidecar_id": "alpha-host",
        "completed_providers": [],
        "entries": [_blocked_entry()],
    }
    assert _post_manifest(client, blocked_body).status_code == 200

    inventory = client.get("/api/v1/system/credentials", headers=_admin_headers())
    assert inventory.status_code == 200, inventory.text
    assert len(inventory.json()["blocked_sources"]) == 1

    readable_body = {
        "sidecar_id": "alpha-host",
        "completed_providers": [],
        "entries": [],
    }
    assert _post_manifest(client, readable_body).status_code == 200

    inventory = client.get("/api/v1/system/credentials", headers=_admin_headers())
    assert inventory.status_code == 200, inventory.text
    assert inventory.json()["blocked_sources"] == []


def test_a_withheld_token_outranks_an_unavailable_source_for_the_same_origin(client, session):
    """One origin can be reported both ways; the louder reason wins."""
    from app.services.credential_tags import PendingCredentialTagRepo

    origin = "cookie:ollama/Chrome/Default/Network/Cookies"
    for entries in (
        [
            _blocked_entry(origin=origin),
            {**_blocked_entry(origin=origin), "reason": "token_withheld"},
        ],
        [
            {**_blocked_entry(origin=origin), "reason": "token_withheld"},
            _blocked_entry(origin=origin),
        ],
    ):
        body = {
            "sidecar_id": "alpha-host",
            "completed_providers": ["ollama"],
            "entries": entries,
        }
        assert _post_manifest(client, body).status_code == 200

        pending = PendingCredentialTagRepo.list_all(session, sidecar_id="alpha-host")
        assert [row.reason for row in pending] == ["token_withheld"]
        # A withheld token is a credential awaiting an account, so it is untagged.
        assert pending[0].source_store is None


def test_an_older_sidecar_is_unaffected(client, session, inventory_session):
    """A manifest without a source reason behaves exactly as before."""
    from app.services.credential_tags import PendingCredentialTagRepo

    body = {
        "sidecar_id": "alpha-host",
        "completed_providers": ["ollama"],
        "entries": [{"provider_id": "ollama", "credential_origin": "provider:ollama"}],
    }
    assert _post_manifest(client, body).status_code == 200

    pending = PendingCredentialTagRepo.list_all(session, sidecar_id="alpha-host")
    assert [row.reason for row in pending] == [None]
    inventory = client.get("/api/v1/system/credentials", headers=_admin_headers())
    assert inventory.json()["blocked_sources"] == []
    assert inventory.json()["unmapped_count"] == 1


def test_an_unusable_source_without_details_is_recorded_bare(client, session):
    """A sidecar that cannot name the store still gets the origin recorded.

    The store, the detail and the remedy are all optional: a report that says
    only "this origin is unreadable" is still true and still worth showing.
    """
    from app.services.credential_tags import PendingCredentialTagRepo

    body = {
        "sidecar_id": "alpha-host",
        "completed_providers": ["ollama"],
        "entries": [
            {
                "provider_id": "ollama",
                "credential_origin": "cookie:ollama/Chrome/Default/Network/Cookies",
                "reason": "source_locked",
            }
        ],
    }
    result = _post_manifest(client, body)
    assert result.status_code == 200, result.text

    pending = PendingCredentialTagRepo.list_all(session, sidecar_id="alpha-host")
    assert len(pending) == 1
    row = pending[0]
    assert row.reason == "source_locked"
    assert row.source_store is None
    assert row.source_detail is None
    assert row.source_remedy is None


def test_a_malformed_source_entry_is_dropped(client, session):
    """A source report without a provider or an origin is not a row.

    There is nothing to key the report on, so it is skipped rather than filed
    against an empty provider.
    """
    from app.services.credential_tags import PendingCredentialTagRepo

    body = {
        "sidecar_id": "alpha-host",
        "completed_providers": ["ollama"],
        "entries": [
            {
                "provider_id": "",
                "credential_origin": "cookie:ollama/Chrome/Default/Network/Cookies",
                "reason": "source_locked",
            },
            {"provider_id": "ollama", "credential_origin": "", "reason": "source_locked"},
        ],
    }
    result = _post_manifest(client, body)
    assert result.status_code == 200, result.text

    assert PendingCredentialTagRepo.list_all(session, sidecar_id="alpha-host") == []
