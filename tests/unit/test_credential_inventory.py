"""The credential inventory: provider → account → source, with machine, mapping,
status and which source is actually feeding the data."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.models.db import (
    CredentialSource,
    CredentialTag,
    LatestUsage,
    PendingCredentialTag,
    PendingUsageEvent,
    ProviderConfig,
    SidecarRegistry,
)
from app.services import credential_inventory
from app.services.credential_inventory import build_inventory, credential_status
from app.services.token_cache import TokenCache

ALICE = "alice@example.com"


@pytest.fixture
def engine(monkeypatch):
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(eng)
    monkeypatch.setattr(credential_inventory, "engine", eng)
    return eng


@pytest.fixture
def cache(monkeypatch) -> TokenCache:
    fresh = TokenCache()
    monkeypatch.setattr(credential_inventory, "token_cache", fresh)
    return fresh


def _source(session: Session, **kw) -> CredentialSource:
    row = CredentialSource(
        **{
            "provider_id": "gemini",
            "account_id": ALICE,
            "source_id": "sidecar:a",
            "source_type": "file",
            "source_label": "oauth_creds.json",
            "credential_origin": "path:/home/u/.gemini/oauth_creds.json",
            "sidecar_id": "host-a",
            **kw,
        }
    )
    session.add(row)
    session.commit()
    return row


def _machine(session: Session, sidecar_id: str, name: str | None = None) -> None:
    session.add(SidecarRegistry(sidecar_id=sidecar_id, hostname=sidecar_id, custom_name=name))
    session.commit()


def _sources(inv, provider="gemini", account=ALICE):
    (prov,) = [p for p in inv.providers if p.provider_id == provider]
    (acct,) = [a for a in prov.accounts if a.account_id == account]
    return acct, {s.source_id: s for s in acct.sources}


@pytest.mark.asyncio
async def test_same_credential_on_every_machine_is_listed_per_machine(engine, cache):
    """The token-health bug: Gemini on four machines showed one row (whichever pushed
    last). Every machine's source must be its own row under the one account."""
    with Session(engine) as s:
        for host in ("dev-01", "macbook", "mgmt", "hermes-01"):
            _machine(s, host, name=host.upper())
            _source(s, source_id=f"sidecar:{host}", sidecar_id=host)
    inv = await build_inventory()

    acct, by_id = _sources(inv)
    assert set(by_id) == {f"sidecar:{h}" for h in ("dev-01", "macbook", "mgmt", "hermes-01")}
    assert {v.machine_name for v in by_id.values()} == {"DEV-01", "MACBOOK", "MGMT", "HERMES-01"}
    assert all(v.origin_kind == "machine" and v.mapping == "local" for v in by_id.values())
    assert {m.machine_id: m.credential_count for m in inv.machines} == {
        "dev-01": 1,
        "macbook": 1,
        "mgmt": 1,
        "hermes-01": 1,
    }
    assert acct.identity_pending is False


@pytest.mark.asyncio
async def test_active_source_is_the_latest_successful_one(engine, cache):
    now = datetime.now(UTC)
    with Session(engine) as s:
        _source(s, source_id="sidecar:a", last_success_at=now - timedelta(minutes=30))
        _source(s, source_id="sidecar:b", sidecar_id="host-b", last_success_at=now)
        _source(s, source_id="sidecar:c", sidecar_id="host-c")  # never succeeded
    acct, by_id = _sources(await build_inventory())

    assert acct.active_source_id == "sidecar:b"
    assert [v.is_active for v in by_id.values()].count(True) == 1
    assert by_id["sidecar:b"].is_active


@pytest.mark.asyncio
async def test_no_provenance_means_no_active_source(engine, cache):
    with Session(engine) as s:
        _source(s)
    acct, _ = _sources(await build_inventory())
    assert acct.active_source_id is None


@pytest.mark.asyncio
async def test_status_comes_from_live_bundle_expiry(engine, cache):
    with Session(engine) as s:
        _source(s, source_id="sidecar:a")
    await cache.store(
        "gemini",
        {
            "oauth_token": "tok",
            "refresh_token": "rt",
            "expiry_date": str(int((datetime.now(UTC) - timedelta(hours=1)).timestamp() * 1000)),
        },
        account_id=ALICE,
        source_id="sidecar:a",
    )
    _, by_id = _sources(await build_inventory())
    row = by_id["sidecar:a"]
    assert row.live is True
    assert row.rollable is True and row.can_refresh is True
    assert row.token_types == ["oauth_token", "refresh_token", "expiry_date"]
    assert row.status == "expired"
    assert row.expires_in_seconds is not None and row.expires_in_seconds < 0


@pytest.mark.asyncio
async def test_unreported_source_is_stale_and_rejected_one_is_invalid(engine, cache):
    old = datetime.now(UTC) - timedelta(days=3)
    with Session(engine) as s:
        _source(
            s,
            source_id="sidecar:gone",
            sidecar_id="gone",
            last_seen=old,
            token_types_json='["oauth_token"]',
        )
        _source(
            s,
            source_id="sidecar:rejected",
            sidecar_id="host-r",
            token_types_json='["api_key"]',
            health="auth_failed",
        )
    _, by_id = _sources(await build_inventory())
    assert by_id["sidecar:gone"].status == "stale"
    assert by_id["sidecar:rejected"].status == "invalid"


@pytest.mark.asyncio
async def test_account_status_is_its_best_enabled_source(engine, cache):
    """A working credential beside a dead one means collection still works."""
    with Session(engine) as s:
        _source(s, source_id="sidecar:ok", token_types_json='["api_key"]')
        _source(
            s,
            source_id="sidecar:bad",
            sidecar_id="host-b",
            health="auth_failed",
            token_types_json='["api_key"]',
        )
    acct, _ = _sources(await build_inventory())
    assert acct.status == "valid"


def test_credential_status_precedence():
    kw = {
        "token_types": ["oauth_token"],
        "rollable": False,
        "machine_sourced": True,
        "last_seen": datetime.now(UTC),
    }
    future = datetime.now(UTC).timestamp() + 90 * 86400
    past = datetime.now(UTC).timestamp() - 60
    assert credential_status(exp=future, health="healthy", live=True, **kw) == "valid"
    assert credential_status(exp=past, health="healthy", live=True, **kw) == "expired"
    # Rejected but otherwise fine → invalid; already expired stays expired.
    assert credential_status(exp=future, health="auth_failed", live=True, **kw) == "invalid"
    assert credential_status(exp=past, health="auth_failed", live=True, **kw) == "expired"
    # Not live and long unreported → stale, whatever the stored expiry says.
    old = {**kw, "last_seen": datetime.now(UTC) - timedelta(days=2)}
    assert credential_status(exp=future, health="healthy", live=False, **old) == "stale"
    assert credential_status(exp=past, health="healthy", live=False, **old) == "stale"
    # A live bundle is never stale, and non-machine sources never are.
    assert credential_status(exp=future, health="healthy", live=True, **old) == "valid"
    assert (
        credential_status(
            exp=future, health="healthy", live=False, **{**old, "machine_sourced": False}
        )
        == "valid"
    )


@pytest.mark.asyncio
async def test_mapping_kinds(engine, cache):
    with Session(engine) as s:
        # operator tag scoped to one machine, plus a deployment-wide one for another origin
        _source(s, source_id="sidecar:tagged", credential_origin="path:/tagged.json")
        s.add(
            CredentialTag(
                provider_id="gemini",
                credential_origin="path:/tagged.json",
                account_id=ALICE,
                sidecar_id="host-a",
                set_by="operator",
            )
        )
        _source(
            s,
            source_id="sidecar:verified",
            sidecar_id="host-v",
            credential_origin="path:/verified.json",
        )
        s.add(
            CredentialTag(
                provider_id="gemini",
                credential_origin="path:/verified.json",
                account_id=ALICE,
                sidecar_id=None,
                set_by="identity_verification",
            )
        )
        _source(
            s, source_id="sidecar:local", sidecar_id="host-l", credential_origin="path:/local.json"
        )
        _source(
            s,
            source_id="config:gemini:alice",
            sidecar_id=None,
            source_type="config",
            source_label="Manual configuration",
            credential_origin=None,
        )
        _source(
            s,
            source_id="server:gemini:env:GEMINI_API_KEY",
            sidecar_id=None,
            source_type="env",
            source_label="GEMINI_API_KEY",
            credential_origin=None,
        )
        _source(
            s,
            source_id="sidecar:pending",
            account_id="default",
            sidecar_id="host-p",
            credential_origin="path:/p.json",
        )
        s.commit()
    inv = await build_inventory()
    _, by_id = _sources(inv)

    assert (by_id["sidecar:tagged"].mapping, by_id["sidecar:tagged"].mapping_scope) == (
        "operator",
        "machine",
    )
    assert (by_id["sidecar:verified"].mapping, by_id["sidecar:verified"].mapping_scope) == (
        "verified",
        "all_machines",
    )
    assert by_id["sidecar:local"].mapping == "local"
    assert by_id["config:gemini:alice"].mapping == "config"
    assert by_id["config:gemini:alice"].origin_kind == "config"
    assert by_id["config:gemini:alice"].token_types == ["api_key"]
    server = by_id["server:gemini:env:GEMINI_API_KEY"]
    assert (server.mapping, server.origin_kind, server.removable) == ("server", "server", False)
    assert server.label == "GEMINI_API_KEY"

    pending_acct, pending = _sources(inv, account="default")
    assert pending["sidecar:pending"].mapping == "pending"
    assert pending_acct.identity_pending is True


@pytest.mark.asyncio
async def test_machine_scoped_tag_beats_deployment_wide(engine, cache):
    with Session(engine) as s:
        _source(s, source_id="sidecar:x", credential_origin="path:/x.json")
        s.add(
            CredentialTag(
                provider_id="gemini",
                credential_origin="path:/x.json",
                account_id=ALICE,
                sidecar_id=None,
                set_by="identity_verification",
            )
        )
        s.add(
            CredentialTag(
                provider_id="gemini",
                credential_origin="path:/x.json",
                account_id=ALICE,
                sidecar_id="host-a",
                set_by="operator",
            )
        )
        s.commit()
    _, by_id = _sources(await build_inventory())
    assert by_id["sidecar:x"].mapping == "operator"


@pytest.mark.asyncio
async def test_labels_counts_and_data_path(engine, cache):
    with Session(engine) as s:
        _machine(s, "host-a")
        _source(s)
        s.add(ProviderConfig(provider_id="gemini", account_id=ALICE, account_label="Work"))
        s.add(
            PendingCredentialTag(
                sidecar_id="host-a", provider_id="chatgpt", credential_origin="path:/auth.json"
            )
        )
        s.add(
            CredentialTag(provider_id="gemini", credential_origin="path:/r.json", account_id=ALICE)
        )
        s.add(
            PendingUsageEvent(
                provider_id="gemini",
                event_id="e1",
                sidecar_id="host-a",
                ts=datetime.now(UTC),
                payload_json="{}",
            )
        )
        s.add(
            LatestUsage(
                provider_id="gemini",
                account_id=ALICE,
                card_json='{"data_source": "api", "input_source": "sidecar"}',
            )
        )
        s.commit()
    inv = await build_inventory()
    acct, _ = _sources(inv)

    assert acct.account_label == "Work"
    assert (acct.data_source, acct.input_source) == ("api", "sidecar")
    assert (inv.unmapped_count, inv.rule_count, inv.pending_usage_events) == (1, 1, 1)
    (machine,) = inv.machines
    assert (machine.credential_count, machine.unmapped_count) == (1, 1)


@pytest.mark.asyncio
async def test_inventory_never_contains_secret_values(engine, cache):
    with Session(engine) as s:
        _source(s)
    await cache.store(
        "gemini",
        {
            "oauth_token": "super-secret-token-value",
            "refresh_token": "super-secret-refresh",
        },  # pragma: allowlist secret
        account_id=ALICE,
        source_id="sidecar:a",
    )
    dumped = (await build_inventory()).model_dump_json()
    assert "super-secret" not in dumped


@pytest.mark.asyncio
async def test_empty_inventory(engine, cache):
    inv = await build_inventory()
    assert inv.providers == [] and inv.machines == []


@pytest.mark.asyncio
async def test_env_and_config_credentials_on_default_are_a_real_account_not_pending(engine, cache):
    """An env-var-only install files everything under ``default``; that is the account,
    not "waiting for an account" (which is only true of a machine-reported credential)."""
    with Session(engine) as s:
        _source(
            s,
            provider_id="openrouter",
            account_id="default",
            source_id="server:openrouter:env:OPENROUTER_API_KEY",
            source_type="env",
            source_label="OPENROUTER_API_KEY",
            sidecar_id=None,
            credential_origin=None,
        )
        _source(
            s,
            provider_id="minimax",
            account_id="default",
            source_id="config:minimax:default",
            source_type="config",
            source_label="Manual configuration",
            sidecar_id=None,
            credential_origin=None,
        )
    inv = await build_inventory()
    for provider in ("openrouter", "minimax"):
        acct, by_id = _sources(inv, provider=provider, account="default")
        assert acct.identity_pending is False
        assert all(not v.identity_pending for v in by_id.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["null", "5", '{"a": 1}', "not json", ""])
async def test_garbage_token_types_never_break_the_inventory(engine, cache, raw):
    with Session(engine) as s:
        _source(s, token_types_json=raw)
    _, by_id = _sources(await build_inventory())
    assert by_id["sidecar:a"].token_types == []


@pytest.mark.asyncio
async def test_data_path_uses_each_accounts_freshest_card(engine, cache):
    """latest_usage has one row per window/variant/model; the data path must come from the
    freshest, chosen in the database (no naive-vs-aware datetime comparison in Python)."""
    now = datetime.now(UTC)
    with Session(engine) as s:
        _source(s)
        _source(s, account_id="bob@example.com", source_id="sidecar:b", sidecar_id="host-b")
        for account, window, minutes_ago, data_source in [
            (ALICE, "session", 90, "local"),  # older, different path
            (ALICE, "weekly", 5, "api"),  # freshest for alice
            (ALICE, "monthly", 30, "web"),
            ("bob@example.com", "session", 10, "web"),
        ]:
            s.add(
                LatestUsage(
                    provider_id="gemini",
                    account_id=account,
                    window_type=window,
                    card_json=f'{{"data_source": "{data_source}", "input_source": "sidecar"}}',
                    updated_at=now - timedelta(minutes=minutes_ago),
                )
            )
        s.commit()
    inv = await build_inventory()

    assert _sources(inv)[0].data_source == "api"
    assert _sources(inv, account="bob@example.com")[0].data_source == "web"


@pytest.mark.asyncio
@pytest.mark.parametrize("card", ["not json", "[1, 2]", "null", ""])
async def test_data_path_tolerates_garbage_card_json(engine, cache, card):
    with Session(engine) as s:
        _source(s)
        s.add(LatestUsage(provider_id="gemini", account_id=ALICE, card_json=card))
        s.commit()
    acct, _ = _sources(await build_inventory())
    assert (acct.data_source, acct.input_source) == (None, None)
