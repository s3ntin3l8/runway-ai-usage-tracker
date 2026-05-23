"""Tests for upsert_latest_usage error-suppression and orphan-eviction logic."""

import json
import os
import tempfile

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.models.db import LatestUsage
from app.services.accumulator import evict_orphan_error_rows, upsert_latest_usage


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


def test_default_error_suppressed_when_real_account_exists(session: Session):
    """Success card under real account; default-orphan error card must be suppressed."""
    upsert_latest_usage(session, _success_card())
    session.commit()

    upsert_latest_usage(session, _error_card())  # account_id="default"
    session.commit()

    rows = session.exec(select(LatestUsage)).all()
    assert len(rows) == 1
    assert rows[0].account_id == "alice@example.com"


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


def test_success_evicts_default_orphan_on_write(session: Session):
    """Writing a success card for a real account deletes an existing default-orphan error row."""
    upsert_latest_usage(session, _error_card())  # persisted first (no healthy row yet)
    session.commit()

    upsert_latest_usage(session, _success_card())
    session.commit()

    rows = session.exec(select(LatestUsage)).all()
    assert len(rows) == 1
    assert rows[0].account_id == "alice@example.com"
    assert json.loads(rows[0].card_json).get("error_type") is None


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
    """evict_orphan_error_rows removes the three-row chatgpt scenario in one shot.

    Rows are inserted directly (bypassing upsert_latest_usage suppression) to
    simulate the pre-fix database state that already exists on disk.
    """
    success_json = json.dumps(_success_card(variant="Codex"))
    error_same_account_json = json.dumps(
        _error_card(
            account_id="alice@example.com", account_label="alice@example.com", variant="default"
        )
    )
    error_default_json = json.dumps(_error_card(account_id="default"))

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

    assert deleted == 2
    rows_after = session.exec(select(LatestUsage)).all()
    assert len(rows_after) == 1
    assert rows_after[0].account_id == "alice@example.com"
    assert json.loads(rows_after[0].card_json).get("error_type") is None


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
