"""Tests for upsert_latest_usage error-suppression and orphan-eviction logic."""

import json
import logging
import os
import tempfile

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.models.db import LatestUsage, LatestUsageContribution
from app.services.accumulator import (
    _parse_source_id,
    evict_orphan_error_rows,
    mark_latest_usage_source_stale,
    reconcile_latest_usage_snapshot,
    upsert_latest_usage,
)


@pytest.fixture(name="session")
def session_fixture():
    fd, db_path = tempfile.mkstemp()
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s
    os.close(fd)
    if os.path.exists(db_path):
        os.remove(db_path)


def _success_card(
    provider_id="chatgpt",
    account_id="alice@example.com",
    account_label="alice@example.com",
    variant="Codex",
    window_type="weekly",
):
    return {
        "service_name": "ChatGPT",
        "icon": "💬",
        "unit": "messages",
        "unit_type": "messages",
        "used_value": 50.0,
        "limit_value": 100.0,
        "health": "good",
        "remaining": "50",
        "detail": "",
        "pace": "normal",
        "reset_in": "3d",
        "window_type": window_type,
        "provider_id": provider_id,
        "account_id": account_id,
        "account_label": account_label,
        "variant": variant,
        "data_source": "api",
        "input_source": "server",
        "sidecar_id": "local",
    }


def _error_card(
    provider_id="chatgpt",
    account_id="default",
    account_label="Default",
    variant="default",
    window_type="weekly",
):
    return {
        "service_name": "ChatGPT Codex",
        "icon": "💬",
        "unit": "ERR",
        "unit_type": "unknown",
        "health": "critical",
        "remaining": "ERR",
        "detail": "No logs/auth found",
        "pace": "Stopped",
        "reset_in": "—",
        "window_type": window_type,
        "provider_id": provider_id,
        "account_id": account_id,
        "account_label": account_label,
        "variant": variant,
        "error_type": "missing_config",
        "data_source": "error",
        "input_source": "server",
        "sidecar_id": "local",
    }


def _rows(session: Session) -> list[dict]:
    return [
        {
            "account_id": r.account_id,
            "variant": r.variant,
            "json": json.loads(r.card_json),
        }
        for r in session.exec(select(LatestUsage)).all()
    ]


# ── Error-suppression tests ───────────────────────────────────────────────────


def test_error_suppressed_when_healthy_row_exists(session: Session):
    """Success card written first; error card for same account must be suppressed."""
    upsert_latest_usage(session, _success_card())
    session.commit()

    upsert_latest_usage(
        session, _error_card(account_id="alice@example.com", account_label="alice@example.com")
    )
    session.commit()

    rows = session.exec(select(LatestUsage)).all()
    assert len(rows) == 1
    assert rows[0].account_id == "alice@example.com"
    assert json.loads(rows[0].card_json).get("error_type") is None


def test_complete_snapshot_retires_only_its_source_and_keeps_history_tables(session: Session):
    server_card = _success_card()
    sidecar_card = _success_card()
    sidecar_card.update(
        {
            "window_type": "session",
            "variant": "sidecar-session",
            "data_source": "local",
            "sidecar_id": "host-a",
        }
    )
    upsert_latest_usage(session, server_card, source_id="server:chatgpt")
    upsert_latest_usage(
        session,
        sidecar_card,
        sidecar_id_override="host-a",
        source_id="sidecar:host-a:chatgpt",
    )
    session.commit()

    removed = reconcile_latest_usage_snapshot(
        session,
        provider_id="chatgpt",
        account_id="alice@example.com",
        source_id="sidecar:host-a:chatgpt",
        reported_keys=set(),
    )
    session.commit()

    rows = session.exec(select(LatestUsage)).all()
    assert removed == 1
    assert [(r.window_type, r.account_id) for r in rows] == [("weekly", "alice@example.com")]
    contributions = session.exec(select(LatestUsageContribution)).all()
    assert [(r.source_id, r.window_type) for r in contributions] == [("server:chatgpt", "weekly")]


def test_real_failure_marks_last_good_source_stale(session: Session):
    upsert_latest_usage(session, _success_card(), source_id="server:chatgpt")
    session.commit()

    changed = mark_latest_usage_source_stale(
        session,
        provider_id="chatgpt",
        source_id="server:chatgpt",
        stale_after_seconds=0,
    )
    session.commit()

    row = session.exec(select(LatestUsage)).one()
    card = json.loads(row.card_json)
    assert changed == 1
    assert card["stale"] is True
    assert card["collection_failing"] is True


def test_fresh_non_quota_card_clears_source_staleness(session: Session):
    card = _success_card()
    upsert_latest_usage(session, card, source_id="server:chatgpt")
    session.commit()
    mark_latest_usage_source_stale(
        session,
        provider_id="chatgpt",
        source_id="server:chatgpt",
        stale_after_seconds=0,
    )
    session.commit()

    token_card = dict(card)
    token_card.update(
        {
            "used_value": None,
            "limit_value": None,
            "pct_used": None,
            "unit": "tokens",
            "unit_type": "tokens",
            "remaining": "42",
            "data_source": "local",
        }
    )
    upsert_latest_usage(session, token_card, source_id="server:chatgpt")
    session.commit()

    stored = json.loads(session.exec(select(LatestUsage)).one().card_json)
    assert stored.get("stale") is not True
    assert stored.get("collection_failing") is not True


def test_server_source_keeps_local_sidecar_id_when_sidecar_writes_later(session: Session):
    server_card = _success_card()
    sidecar_card = {**server_card, "data_source": "local", "sidecar_id": "host-a"}
    upsert_latest_usage(session, server_card, source_id="server:chatgpt")
    upsert_latest_usage(
        session,
        sidecar_card,
        sidecar_id_override="host-a",
        source_id="sidecar:host-a:chatgpt",
    )
    session.commit()

    row = session.exec(select(LatestUsage)).one()
    assert row.sidecar_id == "local"


def test_fresh_sidecar_owns_card_when_server_contribution_is_stale(session: Session):
    server_card = _success_card()
    sidecar_card = {**server_card, "data_source": "local", "sidecar_id": "host-a"}
    upsert_latest_usage(session, server_card, source_id="server:chatgpt")
    mark_latest_usage_source_stale(
        session,
        provider_id="chatgpt",
        source_id="server:chatgpt",
        stale_after_seconds=0,
    )
    upsert_latest_usage(
        session,
        sidecar_card,
        sidecar_id_override="host-a",
        source_id="sidecar:host-a:chatgpt",
    )
    session.commit()

    row = session.exec(select(LatestUsage)).one()
    card = json.loads(row.card_json)
    assert row.sidecar_id == "host-a"
    assert card.get("stale") is not True
    assert card.get("collection_failing") is not True


def test_stale_only_server_card_keeps_local_owner(session: Session):
    upsert_latest_usage(session, _success_card(), source_id="server:chatgpt")
    mark_latest_usage_source_stale(
        session,
        provider_id="chatgpt",
        source_id="server:chatgpt",
        stale_after_seconds=0,
    )
    session.commit()

    row = session.exec(select(LatestUsage)).one()
    assert row.sidecar_id == "local"


def test_when_all_sources_are_stale_latest_sidecar_owns_card(session: Session):
    server_card = _success_card()
    sidecar_card = {**server_card, "data_source": "local", "sidecar_id": "host-a"}
    upsert_latest_usage(session, server_card, source_id="server:chatgpt")
    upsert_latest_usage(
        session,
        sidecar_card,
        sidecar_id_override="host-a",
        source_id="sidecar:host-a:chatgpt",
    )
    mark_latest_usage_source_stale(
        session,
        provider_id="chatgpt",
        source_id="server:chatgpt",
        stale_after_seconds=0,
    )
    mark_latest_usage_source_stale(
        session,
        provider_id="chatgpt",
        source_id="sidecar:host-a:chatgpt",
        stale_after_seconds=0,
    )
    session.commit()

    row = session.exec(select(LatestUsage)).one()
    assert row.sidecar_id == "host-a"
    assert json.loads(row.card_json)["stale"] is True


def test_contribution_write_failure_is_logged(session: Session, monkeypatch, caplog):
    def fail_contribution_write(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr("app.services.accumulator._upsert_contribution", fail_contribution_write)
    with caplog.at_level(logging.WARNING, logger="app.services.accumulator"):
        upsert_latest_usage(session, _success_card(), source_id="server:chatgpt")

    assert "LatestUsage contribution write failed" in caplog.text
    assert "chatgpt/alice@example.com/weekly" in caplog.text
    assert "database unavailable" in caplog.text


def test_source_id_parser_extracts_producer_and_provider():
    assert _parse_source_id("server:chatgpt") == ("server", "local", "chatgpt")
    assert _parse_source_id("sidecar:host-a:anthropic") == (
        "sidecar",
        "host-a",
        "anthropic",
    )


def test_default_error_suppressed_when_real_account_exists_same_slot(session: Session):
    """Same-slot suppression: success card at (chatgpt, alice, weekly, default, "")
    suppresses a default-tagged error card at the same slot."""
    upsert_latest_usage(session, _success_card(account_id="alice@example.com", variant="default"))
    session.commit()

    upsert_latest_usage(session, _error_card())  # account_id="default", variant="default"
    session.commit()

    rows = session.exec(select(LatestUsage)).all()
    assert len(rows) == 1
    assert rows[0].account_id == "alice@example.com"


def test_default_error_kept_when_real_account_is_different_slot(session: Session):
    """Multi-account hardening: a default-tagged error card at one slot is
    NOT suppressed just because a real-account success exists at a different
    slot. Pre-fix code killed this legitimate card."""
    # Real-account success at Codex variant
    upsert_latest_usage(session, _success_card(account_id="alice@example.com", variant="Codex"))
    session.commit()

    # Default-tagged error at the "default" variant — different slot.
    upsert_latest_usage(session, _error_card())  # variant="default"
    session.commit()

    rows = session.exec(select(LatestUsage)).all()
    # Both rows survive: Codex real-account + default-variant error.
    assert len(rows) == 2
    by_variant = {r.variant: r.account_id for r in rows}
    assert by_variant == {"Codex": "alice@example.com", "default": "default"}


def test_error_allowed_when_no_healthy_row(session: Session):
    """Error card is persisted when no healthy row exists for this provider."""
    upsert_latest_usage(session, _error_card())
    session.commit()

    rows = session.exec(select(LatestUsage)).all()
    assert len(rows) == 1
    assert json.loads(rows[0].card_json)["error_type"] == "missing_config"


def test_error_allowed_for_unrelated_provider(session: Session):
    """Error card for provider B is not suppressed by healthy row for provider A."""
    upsert_latest_usage(session, _success_card(provider_id="anthropic"))
    session.commit()

    upsert_latest_usage(session, _error_card(provider_id="chatgpt"))
    session.commit()

    rows = session.exec(select(LatestUsage)).all()
    assert len(rows) == 2


# ── Orphan eviction tests ─────────────────────────────────────────────────────


def test_success_evicts_default_orphan_on_write_same_slot(session: Session):
    """Writing a real-account success card deletes an existing default-tagged
    error row in the same slot."""
    upsert_latest_usage(session, _error_card())  # persisted first (no healthy row yet)
    session.commit()

    upsert_latest_usage(session, _success_card(account_id="alice@example.com", variant="default"))
    session.commit()

    rows = session.exec(select(LatestUsage)).all()
    assert len(rows) == 1
    assert rows[0].account_id == "alice@example.com"
    assert json.loads(rows[0].card_json).get("error_type") is None


def test_success_does_not_evict_default_orphan_in_different_slot(session: Session):
    """Multi-account hardening: a real-account success at one slot must not
    silently evict a default-tagged error card at a different slot."""
    # Default-tagged error at the default variant
    upsert_latest_usage(session, _error_card())
    session.commit()

    # Real-account success at the Codex variant — different slot
    upsert_latest_usage(session, _success_card(account_id="alice@example.com", variant="Codex"))
    session.commit()

    rows = session.exec(select(LatestUsage)).all()
    # Both rows survive: Codex real-account + default-variant default-tagged error
    assert len(rows) == 2
    by_variant = {r.variant: r.account_id for r in rows}
    assert by_variant == {"Codex": "alice@example.com", "default": "default"}


def test_success_evicts_cross_variant_error_on_write(session: Session):
    """Success card (variant=Codex) evicts stale error card with a different variant."""
    stale = _error_card(
        account_id="alice@example.com", account_label="alice@example.com", variant="stale-variant"
    )
    upsert_latest_usage(session, stale)
    session.commit()

    upsert_latest_usage(session, _success_card())  # variant=Codex
    session.commit()

    rows = session.exec(select(LatestUsage)).all()
    assert len(rows) == 1
    assert rows[0].variant == "Codex"


def test_success_preserves_healthy_other_variant(session: Session):
    """Success write does NOT evict another healthy row under a different variant."""
    other_healthy = _success_card(
        variant="Plus", account_id="alice@example.com", account_label="alice@example.com"
    )
    upsert_latest_usage(session, other_healthy)
    session.commit()

    upsert_latest_usage(session, _success_card(variant="Codex"))
    session.commit()

    rows = session.exec(select(LatestUsage)).all()
    assert len(rows) == 2


# ── evict_orphan_error_rows tests ─────────────────────────────────────────────


def test_evict_orphan_error_rows_cleans_existing_stale_rows(session: Session):
    """evict_orphan_error_rows removes the legacy single-account error pattern.

    Setup:
      - Success at (chatgpt, alice, weekly, Codex, "")
      - Same-account error at (chatgpt, alice, weekly, default, "")
      - Default-tagged error at (chatgpt, default, weekly, default, "")

    Rows are inserted directly (bypassing upsert_latest_usage suppression) to
    simulate the pre-fix database state that already exists on disk.

    Multi-account hardened: the same-account error row is evicted (its
    account_id has a healthy row regardless of slot). The default-tagged
    error survives because no real-account success covers its slot.
    """
    success_json = json.dumps(_success_card(variant="Codex"))
    error_same_account_json = json.dumps(
        _error_card(
            account_id="alice@example.com", account_label="alice@example.com", variant="default"
        )
    )
    error_default_json = json.dumps(_error_card(account_id="default", variant="default"))

    session.add(
        LatestUsage(
            provider_id="chatgpt",
            account_id="alice@example.com",
            window_type="weekly",
            variant="Codex",
            model_id="",
            card_json=success_json,
        )
    )
    session.add(
        LatestUsage(
            provider_id="chatgpt",
            account_id="alice@example.com",
            window_type="weekly",
            variant="default",
            model_id="",
            card_json=error_same_account_json,
        )
    )
    session.add(
        LatestUsage(
            provider_id="chatgpt",
            account_id="default",
            window_type="weekly",
            variant="default",
            model_id="",
            card_json=error_default_json,
        )
    )
    session.commit()

    rows_before = session.exec(select(LatestUsage)).all()
    assert len(rows_before) == 3

    deleted = evict_orphan_error_rows(session)
    session.commit()

    # Same-account error (alice/default) evicted by the (pid, aid) rule.
    # Default-tagged error at the "default" variant survives because no
    # real-account success covers that slot (the only success is Codex).
    assert deleted == 1
    rows_after = session.exec(select(LatestUsage)).all()
    assert len(rows_after) == 2
    by_variant = {r.variant: r.account_id for r in rows_after}
    assert by_variant == {"Codex": "alice@example.com", "default": "default"}


def test_evict_orphan_error_rows_evicts_same_slot_default_orphan(session: Session):
    """Same-slot default-orphan eviction: when a real-account success covers
    the same (provider_id, window_type, variant, model_id) slot as a
    default-tagged error, the error row IS evicted."""
    success_json = json.dumps(_success_card(variant="default"))
    error_default_json = json.dumps(_error_card(account_id="default", variant="default"))

    session.add(
        LatestUsage(
            provider_id="chatgpt",
            account_id="alice@example.com",
            window_type="weekly",
            variant="default",
            model_id="",
            card_json=success_json,
        )
    )
    session.add(
        LatestUsage(
            provider_id="chatgpt",
            account_id="default",
            window_type="weekly",
            variant="default",
            model_id="",
            card_json=error_default_json,
        )
    )
    session.commit()

    deleted = evict_orphan_error_rows(session)
    session.commit()

    # Same-slot default-orphan evicted; real-account success survives.
    assert deleted == 1
    rows = session.exec(select(LatestUsage)).all()
    assert len(rows) == 1
    assert rows[0].account_id == "alice@example.com"
    assert rows[0].variant == "default"
    assert json.loads(rows[0].card_json).get("error_type") is None


def test_evict_orphan_error_rows_noop_when_clean(session: Session):
    """evict_orphan_error_rows is a no-op when no orphan error rows exist."""
    upsert_latest_usage(session, _success_card())
    session.commit()

    deleted = evict_orphan_error_rows(session)
    assert deleted == 0


# ── Cross-window-type delete guard tests ──────────────────────────────────────


def _make_card(
    provider_id: str = "anthropic",
    account_id: str = "alice@example.com",
    window_type: str = "session",
    model_id: str | None = None,
    variant: str | None = None,
) -> dict:
    """Minimal valid LimitCard for cross-window-type tests."""
    return {
        "service_name": provider_id.capitalize(),
        "icon": "🤖",
        "unit": "%",
        "unit_type": "percent",
        "pct_used": 40.0,
        "used_value": 40.0,
        "limit_value": 100.0,
        "health": "good",
        "remaining": "60%",
        "detail": "",
        "pace": "normal",
        "reset_in": "3h",
        "window_type": window_type,
        "provider_id": provider_id,
        "account_id": account_id,
        "account_label": account_id,
        "data_source": "api",
        "input_source": "server",
        "sidecar_id": "local",
        **({"model_id": model_id} if model_id is not None else {}),
        **({"variant": variant} if variant is not None else {}),
    }


def test_aggregate_cards_different_window_types_coexist(session: Session):
    """Aggregate cards (model_id='') with different window_types must NOT delete each other.

    Anthropic emits both a 'session' card (five_hour window) and a 'weekly' card
    (seven_day window) — both at the aggregate level with empty model_id. Upserting
    the weekly card must not delete the session card.
    """
    # Seed: Anthropic session aggregate
    upsert_latest_usage(session, _make_card(window_type="session"))
    session.commit()

    # Upsert the weekly aggregate — must NOT delete the session row
    upsert_latest_usage(session, _make_card(window_type="weekly"))
    session.commit()

    rows = session.exec(select(LatestUsage)).all()
    window_types = {r.window_type for r in rows}
    assert window_types == {"session", "weekly"}, (
        f"Expected both session and weekly rows to coexist; found: {window_types}"
    )


def test_model_specific_conflicting_window_type_is_deleted(session: Session):
    """Model-specific cards (model_id != '') must delete a stale same-model row with a
    different window_type.

    Antigravity emits 'session' cards normally and 'weekly' during cooldown for the
    same model_id. Upserting a weekly card for that model must delete the stale session row.
    """
    model = "Claude Opus 4.6 (Thinking)"

    # Seed: stale session row (pre-cooldown)
    upsert_latest_usage(
        session, _make_card(provider_id="antigravity", window_type="session", model_id=model)
    )
    session.commit()

    # Cooldown kicks in: weekly card for the same model arrives
    upsert_latest_usage(
        session, _make_card(provider_id="antigravity", window_type="weekly", model_id=model)
    )
    session.commit()

    rows = session.exec(select(LatestUsage)).all()
    assert len(rows) == 1, f"Expected only the weekly row; found {len(rows)} rows"
    assert rows[0].window_type == "weekly"
