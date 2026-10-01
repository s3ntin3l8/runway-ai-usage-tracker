"""Token Health and the credential inventory must agree on every credential's status.

They are built from different sources (the merged cache + config + server scan vs
``credential_sources``) but share one set of status rules. These tests drive both on the
same world so a divergence — e.g. one view honouring a refresh token or a rejection flag
and the other not — fails here instead of showing up as two screens disagreeing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services import auth_failures, credential_inventory
from app.services import token_health as th
from app.services.token_health import (
    TokenHealthService,
    apply_rejection,
    credential_status,
)
from tests.unit.token_health_world import ALICE, BOB, build_world


async def _both_views(monkeypatch):
    await build_world(monkeypatch)

    monkeypatch.setattr(credential_inventory, "engine", th.engine)
    monkeypatch.setattr(credential_inventory, "token_cache", th.token_cache)
    monkeypatch.setattr(credential_inventory, "_scan_server_credentials", lambda: ({}, set()))
    health = {
        (r["provider"], r["account_id"], r["source_id"]): r["status"]
        for r in await TokenHealthService().get_health()
        if r.get("source_id")
    }
    inv = await credential_inventory.build_inventory()
    inventory = {
        (p.provider_id, a.account_id, s.source_id): s.status
        for p in inv.providers
        for a in p.accounts
        for s in a.sources
        if s.origin_kind == "machine"
    }
    return health, inventory


@pytest.mark.asyncio
async def test_both_views_agree_on_every_machine_credential(monkeypatch):
    health, inventory = await _both_views(monkeypatch)

    # Pending Claude (cache only) has no durable row, so only Token Health lists it.
    shared = {k for k in health if k[2] != "sidecar:pending"}
    assert shared == set(inventory) and shared
    assert {k: health[k] for k in shared} == {k: inventory[k] for k in shared}
    # Spot-check that agreement isn't vacuous: the world covers four distinct outcomes.
    assert {
        ("gemini", ALICE, "sidecar:g1"): "valid",  # refresh token: rolled by the server
        ("gemini", ALICE, "sidecar:g3"): "stale",  # machine went away
        ("openrouter", BOB, "sidecar:o1"): "invalid",  # provider rejected it
    }.items() <= inventory.items()


@pytest.mark.asyncio
async def test_a_durable_auth_failure_marks_the_credential_invalid_in_both_views(monkeypatch):
    """The last collection's auth failure is persisted on the source row; it used to
    colour only the inventory while Token Health (in-memory flags only) said "valid"."""
    await build_world(monkeypatch)
    from sqlmodel import select

    # The suite-wide autouse ``mock_db_session`` fixture replaces ``sqlmodel.Session``, so a
    # call-time ``from sqlmodel import Session`` would hand back a mock and silently write nothing.
    from sqlmodel.orm.session import Session

    from app.models.db import CredentialSource

    auth_failures.reset()  # no in-memory flag: only the durable row says it failed
    with Session(th.engine) as s:
        row = s.exec(
            select(CredentialSource).where(CredentialSource.source_id == "sidecar:g1")
        ).one()
        row.health = "auth_failed"
        s.add(row)
        s.commit()

    monkeypatch.setattr(credential_inventory, "engine", th.engine)
    monkeypatch.setattr(credential_inventory, "token_cache", th.token_cache)
    monkeypatch.setattr(credential_inventory, "_scan_server_credentials", lambda: ({}, set()))
    key = ("gemini", ALICE, "sidecar:g1")
    health = {
        (r["provider"], r["account_id"], r.get("source_id")): r["status"]
        for r in await TokenHealthService().get_health()
    }
    inv = await credential_inventory.build_inventory()
    inventory = {
        (p.provider_id, a.account_id, s.source_id): s.status
        for p in inv.providers
        for a in p.accounts
        for s in a.sources
    }
    assert health[key] == inventory[key] == "invalid"


def test_apply_rejection_upgrades_usable_statuses_only():
    for usable in ("valid", "expiring", "unknown"):
        assert apply_rejection(usable, True) == "invalid"
        assert apply_rejection(usable, False) == usable
    # Already as bad as it gets, or no evidence either way: unchanged.
    assert apply_rejection("expired", True) == "expired"
    assert apply_rejection("stale", True) == "stale"
    assert apply_rejection("invalid", True) == "invalid"


def test_a_rejected_expiring_credential_is_invalid():
    """An "expiring" credential the provider rejected is invalid, not merely expiring.
    (Token Health used to leave it "expiring", so it never alerted.)"""
    soon = datetime.now(UTC).timestamp() + 3600
    kw = {
        "token_types": ["api_key"],
        "rollable": False,
        "live": True,
        "machine_sourced": True,
        "last_seen": datetime.now(UTC),
    }
    assert credential_status(exp=soon, rejected=False, **kw) == "expiring"
    assert credential_status(exp=soon, rejected=True, **kw) == "invalid"


def test_stale_wins_over_everything_for_an_unreported_machine_credential():
    kw = {
        "token_types": ["oauth_token"],
        "rollable": False,
        "live": False,
        "machine_sourced": True,
        "last_seen": datetime.now(UTC) - timedelta(days=2),
    }
    assert credential_status(exp=None, rejected=True, **kw) == "stale"


@pytest.mark.asyncio
async def test_a_rejection_flag_upgrades_usable_credentials_but_not_expired_or_stale(monkeypatch):
    """Through the real get_health: flagging an account marks each of its usable credentials
    ``invalid`` while ``expired`` and ``stale`` rows keep their (already bad / unknown)
    status — the one rule, applied to every row kind."""
    await build_world(monkeypatch)
    auth_failures.mark("gemini", ALICE)
    auth_failures.mark("chatgpt", ALICE)

    rows = {
        (r["provider"], r["account_id"], r.get("source_id")): r["status"]
        for r in await TokenHealthService().get_health()
    }
    assert rows[("gemini", ALICE, "sidecar:g1")] == "invalid"  # was valid
    assert rows[("gemini", ALICE, "sidecar:g2")] == "invalid"  # was valid
    assert rows[("gemini", ALICE, "sidecar:g3")] == "stale"  # unreported: unchanged
    assert rows[("chatgpt", ALICE, None)] == "expired"  # already expired: unchanged


# --- a durable auth failure only counts while it is still the account's story -----------


def _src(source_id="a", **kw):
    from app.models.db import CredentialSource

    return CredentialSource(
        provider_id="gemini",
        account_id=ALICE,
        source_id=source_id,
        source_type="file",
        source_label="creds.json",
        **kw,
    )


def test_is_durably_rejected_truth_table():
    from app.services.token_health import is_durably_rejected

    now = datetime.now(UTC)
    failed = _src("a", health="auth_failed", last_attempt_at=now - timedelta(minutes=5))

    assert is_durably_rejected(failed, []) is True  # nothing else to fall back on
    assert is_durably_rejected(_src("a", health="healthy"), []) is False
    # A disabled source is never tried again, so its last failure must not stand forever.
    assert is_durably_rejected(_src("a", health="auth_failed", enabled=False), []) is False

    working_later = _src("b", last_success_at=now)
    working_earlier = _src("b", last_success_at=now - timedelta(hours=1))
    disabled_sibling = _src("b", last_success_at=now, enabled=False)
    # Failover moved on to a sibling that has succeeded since: collection works.
    assert is_durably_rejected(failed, [working_later]) is False
    # ...but a success from *before* the failure doesn't vouch for the account now.
    assert is_durably_rejected(failed, [working_earlier]) is True
    # A disabled sibling isn't being collected, so its old success proves nothing.
    assert is_durably_rejected(failed, [disabled_sibling]) is True
    # The source itself (same id) is never its own sibling.
    assert is_durably_rejected(failed, [failed]) is True
    # A row from before provenance existed has no attempt time: any success supersedes it.
    legacy = _src("a", health="auth_failed")
    assert is_durably_rejected(legacy, [working_earlier]) is False
    assert is_durably_rejected(legacy, [_src("b")]) is True  # sibling never succeeded
    # Naive datetimes (what SQLite hands back) compare fine against aware ones.
    naive = _src("b", last_success_at=now.replace(tzinfo=None))
    assert is_durably_rejected(failed, [naive]) is False


async def _set_health(th, source_id, **fields):
    from sqlmodel import select
    from sqlmodel.orm.session import Session

    from app.models.db import CredentialSource

    with Session(th.engine) as s:
        row = s.exec(select(CredentialSource).where(CredentialSource.source_id == source_id)).one()
        for key, value in fields.items():
            setattr(row, key, value)
        s.add(row)
        s.commit()


async def _statuses():
    return {
        r.get("source_id"): r["status"]
        for r in await TokenHealthService().get_health()
        if r["provider"] == "gemini"
    }


@pytest.mark.asyncio
async def test_failed_over_source_does_not_stay_invalid_while_a_sibling_works(monkeypatch):
    """Source A is rejected, failover moves to B which succeeds. A keeps ``auth_failed`` (only
    A's own attempt rewrites it) but the account collects fine — banners and alerts must not
    report a rejected credential forever."""
    await build_world(monkeypatch)

    auth_failures.reset()
    now = datetime.now(UTC)
    await _set_health(
        th, "sidecar:g1", health="auth_failed", last_attempt_at=now - timedelta(minutes=2)
    )
    assert (await _statuses())["sidecar:g1"] == "invalid"  # nothing else has worked yet

    await _set_health(th, "sidecar:g2", last_success_at=now)
    statuses = await _statuses()
    assert statuses["sidecar:g1"] == "valid"  # superseded by g2's later success
    assert statuses["sidecar:g2"] == "valid"


@pytest.mark.asyncio
async def test_disabled_source_with_a_stored_auth_failure_is_not_invalid(monkeypatch):
    await build_world(monkeypatch)

    auth_failures.reset()
    await _set_health(th, "sidecar:g1", health="auth_failed", enabled=False)
    assert (await _statuses())["sidecar:g1"] == "valid"


@pytest.mark.asyncio
async def test_stale_source_with_a_stored_auth_failure_stays_stale(monkeypatch):
    await build_world(monkeypatch)

    auth_failures.reset()
    await _set_health(th, "sidecar:g3", health="auth_failed")
    assert (await _statuses())["sidecar:g3"] == "stale"


@pytest.mark.asyncio
async def test_no_internal_keys_leak_into_the_rows(monkeypatch):
    """``_rejected`` / ``_assumed`` / ``_rollable`` are scaffolding for the status passes."""
    await build_world(monkeypatch)

    await _set_health(th, "sidecar:g1", health="auth_failed")
    rows = await TokenHealthService().get_health()
    assert rows
    assert [k for r in rows for k in r if k.startswith("_")] == []


@pytest.mark.asyncio
async def test_inventory_applies_the_same_supersession_rule(monkeypatch):
    """Both views must treat a failed-over source identically (the original divergence)."""
    await build_world(monkeypatch)

    auth_failures.reset()
    now = datetime.now(UTC)
    await _set_health(
        th, "sidecar:g1", health="auth_failed", last_attempt_at=now - timedelta(minutes=2)
    )
    await _set_health(th, "sidecar:g2", last_success_at=now)
    monkeypatch.setattr(credential_inventory, "engine", th.engine)
    monkeypatch.setattr(credential_inventory, "token_cache", th.token_cache)
    monkeypatch.setattr(credential_inventory, "_scan_server_credentials", lambda: ({}, set()))

    inv = await credential_inventory.build_inventory()
    inventory = {
        s.source_id: s.status for p in inv.providers for a in p.accounts for s in a.sources
    }
    assert inventory["sidecar:g1"] == (await _statuses())["sidecar:g1"] == "valid"


# --- is_flagged's "sole account" rule fed from two different row sets ------------------
#
# A flagged ``default`` (a pasted/env key was rejected) matches the default/config/server
# rows, and a non-default account only when it is the provider's sole account. Token Health
# builds "the provider's accounts" from the rows it emits; the inventory from durable
# ``credential_sources``. These pin that the two sets give the same answer end to end.


async def _openrouter_views(monkeypatch, *, with_config: bool):
    """Both views' statuses for openrouter's machine key (BOB) and its pasted config key."""
    from sqlmodel import select
    from sqlmodel.orm.session import Session

    from app.models.db import CredentialSource, ProviderConfig

    await build_world(monkeypatch)
    auth_failures.reset()
    auth_failures.mark("openrouter", "default")  # the pasted/env key was rejected
    with Session(th.engine) as s:
        if with_config:
            # Mirrors what saving a key does: a durable config source beside the ProviderConfig.
            s.add(
                CredentialSource(
                    provider_id="openrouter",
                    account_id="default",
                    source_id="config:openrouter:default",
                    source_type="config",
                    source_label="Manual configuration",
                )
            )
        else:
            for cfg in s.exec(
                select(ProviderConfig).where(ProviderConfig.provider_id == "openrouter")
            ):
                s.delete(cfg)
        s.commit()
    monkeypatch.setattr(credential_inventory, "engine", th.engine)
    monkeypatch.setattr(credential_inventory, "token_cache", th.token_cache)
    monkeypatch.setattr(credential_inventory, "_scan_server_credentials", lambda: ({}, set()))

    health = {
        (r["account_id"], r.get("source_id")): r["status"]
        for r in await TokenHealthService().get_health()
        if r["provider"] == "openrouter"
    }
    inv = await credential_inventory.build_inventory()
    inventory = {
        (a.account_id, s.source_id): s.status
        for p in inv.providers
        if p.provider_id == "openrouter"
        for a in p.accounts
        for s in a.sources
    }
    return health, inventory


@pytest.mark.asyncio
async def test_flagged_default_beside_a_config_key_does_not_flag_the_machine_account(monkeypatch):
    health, inventory = await _openrouter_views(monkeypatch, with_config=True)

    # Token Health names the pasted key "config:default"; the inventory files it under the
    # real account id with its config source id.
    assert health[("config:default", None)] == "invalid"
    assert inventory[("default", "config:openrouter:default")] == "invalid"
    # The provider has two accounts, so bob is not "the sole account" in either view.
    assert health[(BOB, "sidecar:o1")] == inventory[(BOB, "sidecar:o1")] == "valid"


@pytest.mark.asyncio
async def test_flagged_default_flags_a_providers_sole_machine_account(monkeypatch):
    """With no config key, bob is openrouter's only account, so a rejected ``default`` (the
    unscoped credential a lone opaque key was pushed under) applies to it — in both views."""
    health, inventory = await _openrouter_views(monkeypatch, with_config=False)

    assert health[(BOB, "sidecar:o1")] == inventory[(BOB, "sidecar:o1")] == "invalid"
