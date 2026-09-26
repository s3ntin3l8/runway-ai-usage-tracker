"""Account-id canonicalization across the server and the sidecar.

One rule (``canonical_account_id``: blank → "default", emails lowercased,
opaque ids verbatim) must hold on every write path — cards, events,
token-cache keys, provider configs — and in the sidecar's mirror, so the same
account never splits on formatting alone. Also covers the one-shot startup
repair of rows written before the rule existed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.models.db import (
    LatestUsage,
    ProviderConfig,
    QuotaSnapshot,
    UsageEvent,
    UsagePeriodRollup,
)
from app.models.schemas import UsageEventPush
from app.services.account_canonicalization import canonicalize_stored_account_ids
from app.services.account_identity import canonical_account_id, resolve_account_id
from app.services.event_ingestor import EventIngestor
from app.services.token_cache import TokenCache
from scripts.sidecar_pkg import identity as sidecar_identity
from scripts.sidecar_pkg.event_watermark import EventWatermark

CASES = [
    (None, "default"),
    ("", "default"),
    ("   ", "default"),
    ("Alice@Example.COM", "alice@example.com"),
    ("  bob@x.io ", "bob@x.io"),
    ("default", "default"),
    ("5f2c9aBC", "5f2c9aBC"),  # opaque ids are case-sensitive
    ("org-Uuid-123", "org-Uuid-123"),
]


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


# ---------------------------------------------------------------------------
# The rule itself + server/sidecar parity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("raw", "expected"), CASES)
def test_canonical_account_id(raw, expected):
    assert canonical_account_id(raw) == expected


@pytest.mark.parametrize(("raw", "_expected"), CASES)
def test_sidecar_mirror_matches_server(raw, _expected):
    assert sidecar_identity.canonical_account_id(raw) == canonical_account_id(raw)


def test_resolve_account_id_uses_the_same_rule_for_raw_ids():
    assert resolve_account_id("anthropic", "Alice@Example.com", None) == "alice@example.com"
    assert resolve_account_id("anthropic", " abc123 ", None) == "abc123"
    assert resolve_account_id("anthropic", "default", None) == "default"


# ---------------------------------------------------------------------------
# Write paths
# ---------------------------------------------------------------------------


def _push(account_id: str, event_id: str = "e1", **kw) -> UsageEventPush:
    return UsageEventPush(
        provider_id="anthropic",
        account_id=account_id,
        event_id=event_id,
        ts=kw.pop("ts", "2026-09-01T10:00:00Z"),
        kind="message",
        model_id="sonnet-4.5",
        tokens_input=kw.pop("tokens_input", 10),
        tokens_output=5,
        **kw,
    )


def test_event_ingestor_stores_canonical_account_id(session: Session):
    res = EventIngestor(session).ingest([_push("Alice@Example.com")], sidecar_id="laptop")
    assert res.events_inserted == 1
    ev = session.exec(select(UsageEvent)).one()
    assert ev.account_id == "alice@example.com"
    # The same message re-sent with different casing is a duplicate, not a
    # second event on a twin account.
    res2 = EventIngestor(session).ingest([_push("ALICE@example.com")], sidecar_id="laptop")
    assert res2.events_inserted == 0
    assert res2.events_duplicate == 1


async def test_token_cache_keys_are_canonical():
    cache = TokenCache()
    stored = await cache.store("anthropic", {"oauth_token": "t"}, account_id="Alice@X.com")
    assert stored == "alice@x.com"
    assert await cache.get("anthropic", "ALICE@x.com") == {"oauth_token": "t"}
    removed = await cache.remove("anthropic", "Alice@X.com")
    assert removed


def test_provider_config_put_stores_canonical_id(session: Session):
    from fastapi.testclient import TestClient

    from app.core.db import get_session
    from app.main import app

    app.dependency_overrides[get_session] = lambda: session
    try:
        client = TestClient(app)
        resp = client.put(
            "/api/v1/system/provider-config/anthropic/Alice@Example.com",
            json={"enabled": True},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["account_id"] == "alice@example.com"
        rows = session.exec(select(ProviderConfig)).all()
        assert [r.account_id for r in rows] == ["alice@example.com"]

        # DELETE accepts the non-canonical spelling of the canonical row.
        resp = client.delete("/api/v1/system/provider-config/anthropic/ALICE@example.com")
        assert resp.status_code == 200, resp.text
    finally:
        app.dependency_overrides.clear()


def test_provider_config_put_preserves_opencode_cookie_and_workspace(session: Session):
    """OpenCode cookie and workspace settings persist in the account row."""
    from fastapi.testclient import TestClient

    from app.core.db import get_session
    from app.main import app

    app.dependency_overrides[get_session] = lambda: session
    try:
        client = TestClient(app)
        resp = client.put(
            "/api/v1/system/provider-config/opencode/default",
            json={
                "session_cookie": "auth=oc_x; __Host-console_session=st_y",
                "opencode_workspace_id": "workspace-123",
            },
        )
        assert resp.status_code == 200, resp.text

        # DB keeps the full pasted blob so nothing is lost on round-trip.
        row = session.exec(
            select(ProviderConfig).where(
                ProviderConfig.provider_id == "opencode",
                ProviderConfig.account_id == "default",
            )
        ).one()
        assert row.session_cookie == "auth=oc_x; __Host-console_session=st_y"
        assert row.opencode_workspace_id == "workspace-123"
        configs = client.get("/api/v1/system/provider-configs").json()["providers"]
        opencode = next(config for config in configs if config["provider_id"] == "opencode")
        assert opencode["opencode_workspace_id"] == "workspace-123"
        assert opencode["accounts"][0]["opencode_workspace_id"] == "workspace-123"

        # A bare ``auth=`` paste (no console cookie) still collapses to the value.
        resp = client.put(
            "/api/v1/system/provider-config/opencode/default",
            json={"session_cookie": "auth=oc_z"},
        )
        assert resp.status_code == 200, resp.text
        session.refresh(row)
        assert row.session_cookie == "oc_z"
        assert row.opencode_workspace_id == "workspace-123"
    finally:
        app.dependency_overrides.clear()


def test_provider_config_put_stores_xai_bearer_as_access_only(session: Session):
    """A manually pasted xAI bearer is not a refresh token."""
    from fastapi.testclient import TestClient

    from app.api.endpoints.system import token_cache
    from app.core.db import get_session
    from app.main import app

    app.dependency_overrides[get_session] = lambda: session
    try:
        with patch.object(token_cache, "store", new_callable=AsyncMock) as store:
            resp = TestClient(app).put(
                "/api/v1/system/provider-config/xai/default",
                json={"api_key": "xai-test-access"},
            )

        assert resp.status_code == 200, resp.text
        assert store.call_args.args[:2] == ("xai", {"xai_access": "xai-test-access"})
        assert store.call_args.kwargs["source"] == "config"
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Startup repair of legacy rows
# ---------------------------------------------------------------------------


def _event(account_id: str, event_id: str, tokens: int = 10) -> UsageEvent:
    return UsageEvent(
        provider_id="anthropic",
        account_id=account_id,
        sidecar_id="laptop",
        event_id=event_id,
        ts=datetime(2026, 9, 1, 10, tzinfo=UTC),
        kind="message",
        model_id="sonnet-4.5",
        tokens_input=tokens,
    )


def _lifetime_input(session: Session, account_id: str) -> int:
    row = session.exec(
        select(UsagePeriodRollup).where(
            UsagePeriodRollup.account_id == account_id,
            UsagePeriodRollup.period_type == "lifetime",
            UsagePeriodRollup.model_id == "",
            UsagePeriodRollup.sidecar_id == "",
        )
    ).first()
    return row.tokens_input if row else 0


def test_repair_merges_case_split_events_and_rebuilds_rollups(session: Session):
    from sqlalchemy import text

    from app.services.period_rollups import update_rollups_for_event

    # Simulate a pre-migration DB (the (provider, event_id) unique index
    # would otherwise reject the legacy twin outright).
    session.execute(text("DROP INDEX uq_usage_events_provider_event"))
    # e1 exists under both spellings (the double count); e2 only mixed-case.
    for ev in (
        _event("alice@example.com", "e1"),
        _event("Alice@Example.com", "e1"),
        _event("Alice@Example.com", "e2", tokens=7),
    ):
        session.add(ev)
        session.flush()
        update_rollups_for_event(session, ev)
    session.commit()

    report = canonicalize_stored_account_ids(session)

    events = session.exec(select(UsageEvent)).all()
    assert sorted((e.account_id, e.event_id) for e in events) == [
        ("alice@example.com", "e1"),
        ("alice@example.com", "e2"),
    ]
    assert report.dropped == {"usage_events": 1}
    # Rollups are rebuilt from the surviving events: 10 + 7, no twin row.
    assert _lifetime_input(session, "alice@example.com") == 17
    assert _lifetime_input(session, "Alice@Example.com") == 0

    # Idempotent.
    again = canonicalize_stored_account_ids(session)
    assert not again.changed
    assert again.rebuilt_pairs == []


def test_repair_drops_stale_card_twin_and_renames_snapshots(session: Session):
    now = datetime(2026, 9, 1, tzinfo=UTC)
    for aid in ("alice@example.com", "Alice@Example.com"):
        session.add(
            LatestUsage(
                provider_id="anthropic",
                account_id=aid,
                window_type="session",
                service_name="Claude",
                card_json="{}",
                updated_at=now,
            )
        )
    session.add(
        QuotaSnapshot(
            provider_id="anthropic",
            account_id="Alice@Example.com",
            window_type="session",
            ts=now,
            pct_used=12.0,
        )
    )
    session.commit()

    canonicalize_stored_account_ids(session)

    cards = session.exec(select(LatestUsage)).all()
    assert [c.account_id for c in cards] == ["alice@example.com"]
    snaps = session.exec(select(QuotaSnapshot)).all()
    assert [s.account_id for s in snaps] == ["alice@example.com"]


def test_repair_reports_but_keeps_conflicting_provider_configs(session: Session):
    session.add(ProviderConfig(provider_id="anthropic", account_id="alice@example.com"))
    session.add(ProviderConfig(provider_id="anthropic", account_id="Alice@Example.com"))
    session.add(ProviderConfig(provider_id="gemini", account_id="Bob@X.com"))
    session.commit()

    report = canonicalize_stored_account_ids(session)

    ids = sorted((r.provider_id, r.account_id) for r in session.exec(select(ProviderConfig)))
    assert ids == [
        ("anthropic", "Alice@Example.com"),  # conflict — left for the operator
        ("anthropic", "alice@example.com"),
        ("gemini", "bob@x.com"),  # no conflict — renamed
    ]
    assert report.conflicts == {"provider_configs": 1}


# ---------------------------------------------------------------------------
# Sidecar
# ---------------------------------------------------------------------------


def test_watermark_folds_legacy_non_canonical_keys(tmp_path: Path):
    path = tmp_path / "wm.json"
    path.write_text(
        json.dumps(
            {
                "last_pushed_ts": {
                    "anthropic|Alice@Example.com": "2026-09-01T10:00:00+00:00",
                    "anthropic|alice@example.com": "2026-08-01T10:00:00+00:00",
                }
            }
        )
    )
    wm = EventWatermark(path)
    # The newest of the folded keys wins, and lookups are case-insensitive
    # for emails — no bootstrap re-extraction after the upgrade.
    assert wm.last_pushed("anthropic", "ALICE@example.com") == datetime(2026, 9, 1, 10, tzinfo=UTC)


class TestSidecarIdentityPrecedence:
    @staticmethod
    def _ag_config(tmp_path: Path) -> dict:
        tok = tmp_path / "antigravity-oauth-token"
        tok.write_text(json.dumps({"token": {"access_token": "ya29.x", "refresh_token": "1//r"}}))
        return {
            "name": "Antigravity",
            "icon": "x",
            "rules": [
                {
                    "type": "file",
                    "paths": [str(tok)],
                    "format": "json",
                    "mapping": {
                        "token.access_token": "oauth_token",
                        "token.refresh_token": "refresh_token",
                    },
                }
            ],
        }

    def test_operator_hint_overrides_default_identity(self, tmp_path):
        import scripts.sidecar as sidecar

        with patch.dict(sidecar._ACCOUNT_IDENTITIES, {}, clear=True):
            cards, blocked = sidecar.GenericCollector.collect_provider(
                "antigravity",
                self._ag_config(tmp_path),
                account_label_hints={
                    "antigravity": {"provider:antigravity": "me@example.com"},
                },
            )
        token_cards = [c for c in cards if c.get("remaining") == "Token"]
        assert [c["account_id"] for c in token_cards] == ["me@example.com"]
        assert blocked == []

    def test_anthropic_cli_token_card_stamped_with_cli_email(self, tmp_path, monkeypatch):
        import scripts.sidecar as sidecar

        creds = tmp_path / ".credentials.json"
        creds.write_text(
            json.dumps(
                {
                    "claudeAiOauth": {"accessToken": "sk-ant-x"},
                    "oauthAccount": {"emailAddress": "Alice@Example.com"},
                }
            )
        )
        config = {
            "name": "Claude",
            "icon": "x",
            "rules": [
                {
                    "type": "file",
                    "paths": [str(creds)],
                    "format": "json",
                    "mapping": {"claudeAiOauth.accessToken": "oauth_token"},
                }
            ],
        }
        cards, blocked = sidecar.GenericCollector.collect_provider("anthropic", config)
        token_cards = [c for c in cards if c.get("remaining") == "Token"]
        # Same (canonical) identity the anthropic events are stamped with.
        assert [c["account_id"] for c in token_cards] == ["alice@example.com"]
        assert blocked == []

    def test_anthropic_stamp_skipped_when_browser_cookie_collected(self, tmp_path, monkeypatch):
        """A claude.ai cookie may belong to another account than the CLI
        login — the card stays on the tag/hint path instead."""
        import scripts.sidecar as sidecar

        creds = tmp_path / ".credentials.json"
        creds.write_text(
            json.dumps(
                {
                    "claudeAiOauth": {"accessToken": "sk-ant-x"},
                    "oauthAccount": {"emailAddress": "cli@example.com"},
                }
            )
        )
        config = {
            "name": "Claude",
            "icon": "x",
            "rules": [
                {
                    "type": "file",
                    "paths": [str(creds)],
                    "format": "json",
                    "mapping": {"claudeAiOauth.accessToken": "oauth_token"},
                }
            ],
        }
        # A browser cookie collected alongside the CLI token must stay unresolved.
        config["rules"].append(
            {
                "type": "cookie",
                "domains": ["claude.ai"],
                "name": "sessionKey",
                "mapping": {"value": "cookie_sessionKey"},
            }
        )
        monkeypatch.setattr(
            sidecar.BrowserCookieExtractor,
            "get_cookie",
            staticmethod(lambda _domain, _name: "sk-ant-sid01-x"),
        )
        cards, blocked = sidecar.GenericCollector.collect_provider("anthropic", config)
        token_cards = [c for c in cards if c.get("remaining") == "Token"]
        assert [c["account_id"] for c in token_cards] == ["cli@example.com"]
        assert blocked == [
            {"provider_id": "anthropic", "credential_origin": "cookie:anthropic/session"}
        ]

    def test_anthropic_keychain_identity_comes_from_its_own_payload(self, monkeypatch):
        from types import SimpleNamespace

        import scripts.sidecar as sidecar

        monkeypatch.setattr(sidecar.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(
            sidecar.subprocess,
            "run",
            lambda *_args, **_kwargs: SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "claudeAiOauth": {"accessToken": "sk-ant-keychain"},
                        "oauthAccount": {"emailAddress": "Keychain@Example.com"},
                    }
                ),
            ),
        )
        cards, blocked = sidecar.GenericCollector.collect_provider(
            "anthropic",
            {
                "name": "Claude",
                "rules": [
                    {
                        "type": "keychain",
                        "service_name": "Claude Code-credentials",
                        "format": "json",
                        "mapping": {
                            "claudeAiOauth.accessToken": "oauth_token",
                        },
                    }
                ],
            },
        )
        assert [card["account_id"] for card in cards] == ["keychain@example.com"]
        assert blocked == []

    def test_gemini_stamp_reads_collected_id_token(self, tmp_path, monkeypatch):
        """Covers creds under {{CONFIG_DIR:gemini}}, not just ~/.gemini."""
        import base64

        import scripts.sidecar as sidecar

        claims = base64.urlsafe_b64encode(json.dumps({"email": "G@Example.com"}).encode())
        id_token = "h." + claims.decode().rstrip("=") + ".s"
        creds = tmp_path / "oauth_creds.json"
        creds.write_text(json.dumps({"access_token": "ya29.x", "id_token": id_token}))
        config = {
            "name": "Gemini",
            "icon": "x",
            "rules": [
                {
                    "type": "file",
                    "paths": [str(creds)],
                    "format": "json",
                    "mapping": {"access_token": "oauth_token", "id_token": "id_token"},
                }
            ],
        }
        monkeypatch.setattr(sidecar, "_gemini_account_email", lambda: "default")
        cards, _blocked = sidecar.GenericCollector.collect_provider("gemini", config)
        token_cards = [c for c in cards if c.get("remaining") == "Token"]
        assert [c["account_id"] for c in token_cards] == ["g@example.com"]

    def test_opencode_local_db_beats_server_identity(self, tmp_path):
        import sqlite3

        import scripts.sidecar as sidecar

        db = tmp_path / "opencode.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE account (email TEXT)")
        conn.execute("INSERT INTO account VALUES ('local@example.com')")
        conn.commit()
        conn.close()
        with patch.dict(
            sidecar._ACCOUNT_IDENTITIES, {"opencode": "other-host@example.com"}, clear=True
        ):
            assert sidecar._opencode_account_email(db) == "local@example.com"
            # A server-side card cannot identify the account on this host.
            assert sidecar._opencode_account_email(None) == "default"


# ---------------------------------------------------------------------------
# Server-propagated legacy ``identities`` — only when unambiguous
# ---------------------------------------------------------------------------


def _card(session: Session, provider_id: str, account_id: str) -> None:
    session.add(
        LatestUsage(
            provider_id=provider_id,
            account_id=account_id,
            window_type="session",
            service_name=provider_id,
            card_json="{}",
            updated_at=datetime.now(UTC),
        )
    )


def test_identities_only_for_single_account_providers(session: Session):
    from app.api.endpoints.fleet import _get_active_identities
    from app.models.db import SidecarRegistry

    session.add(SidecarRegistry(sidecar_id="laptop", hostname="laptop"))
    _card(session, "antigravity", "me@example.com")
    _card(session, "opencode", "a@example.com")
    _card(session, "opencode", "b@example.com")
    session.commit()

    # Server card identities are never used as sidecar attribution hints.
    assert _get_active_identities(session) == {}


def test_identities_withheld_in_multi_sidecar_deployments(session: Session):
    from app.api.endpoints.fleet import _get_active_identities
    from app.models.db import SidecarRegistry

    session.add(SidecarRegistry(sidecar_id="laptop", hostname="laptop"))
    session.add(SidecarRegistry(sidecar_id="desktop", hostname="desktop"))
    _card(session, "antigravity", "me@example.com")
    session.commit()

    assert _get_active_identities(session) == {}
