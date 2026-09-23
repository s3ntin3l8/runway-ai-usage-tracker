# tests/unit/test_scrub_residual_stale_health.py
"""Startup scrub for residual pre-#293 collection-failure cards."""

import json
import os
import tempfile
from datetime import UTC, datetime

from sqlalchemy import text
from sqlmodel import SQLModel, create_engine

from app.core.db import _scrub_residual_stale_health


def _engine_and_conn():
    fd, db_path = tempfile.mkstemp()
    os.close(fd)
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    conn = engine.connect()
    return engine, conn, db_path


def _insert(conn, card: dict) -> int:
    conn.execute(
        text(
            "INSERT INTO latest_usage "
            "(provider_id, account_id, sidecar_id, window_type, variant, model_id, "
            " card_json, updated_at) "
            "VALUES (:p, :a, 'local', :w, '', '', :c, :u)"
        ),
        {
            "p": "ollama",
            "a": "default",
            "w": "weekly",
            "c": json.dumps(card),
            "u": datetime.now(UTC),
        },
    )
    conn.commit()
    row = conn.execute(text("SELECT id FROM latest_usage")).first()
    return int(row[0])


def _read(conn, row_id: int) -> dict:
    row = conn.execute(
        text("SELECT card_json FROM latest_usage WHERE id = :id"), {"id": row_id}
    ).first()
    return json.loads(row[0])


def test_scrub_sets_stale_and_reconciles_residual_critical():
    engine, conn, db_path = _engine_and_conn()
    try:
        row_id = _insert(
            conn,
            {
                "service_name": "Ollama",
                "pct_used": 0.0,
                "health": "critical",
                "detail": "⚠ Collection failing — timeout [Cached 346.1m ago]",
            },
        )
        _scrub_residual_stale_health(conn)
        card = _read(conn, row_id)
        assert card["stale"] is True
        assert card["collection_failing"] is True
        assert card["health"] == "good"
    finally:
        conn.close()
        engine.dispose()
        if os.path.exists(db_path):
            os.remove(db_path)


def test_scrub_keeps_genuine_critical_and_is_idempotent():
    engine, conn, db_path = _engine_and_conn()
    try:
        row_id = _insert(
            conn,
            {
                "service_name": "Claude",
                "pct_used": 96.0,
                "health": "critical",
                "detail": "⚠ Collection failing — timeout [Cached 2m ago]",
                "stale": True,
            },
        )
        _scrub_residual_stale_health(conn)
        card = _read(conn, row_id)
        assert card["stale"] is True
        assert card["health"] == "critical"

        # Second run: no further rewrite needed (stale already set, health matches)
        before = json.dumps(card, sort_keys=True)
        _scrub_residual_stale_health(conn)
        after = json.dumps(_read(conn, row_id), sort_keys=True)
        assert before == after
    finally:
        conn.close()
        engine.dispose()
        if os.path.exists(db_path):
            os.remove(db_path)


def test_scrub_ignores_healthy_rows_without_failure_detail():
    engine, conn, db_path = _engine_and_conn()
    try:
        row_id = _insert(
            conn,
            {
                "service_name": "Ollama",
                "pct_used": 12.0,
                "health": "good",
                "detail": "All good",
            },
        )
        _scrub_residual_stale_health(conn)
        card = _read(conn, row_id)
        assert "stale" not in card
        assert "collection_failing" not in card
        assert card["health"] == "good"
    finally:
        conn.close()
        engine.dispose()
        if os.path.exists(db_path):
            os.remove(db_path)


def test_init_db_runs_residual_scrub(monkeypatch):
    """init_db must invoke the scrub on startup — deleting the wiring would
    leave residual pre-#293 rows stuck critical with no unit-test coverage."""
    from app.core.db import init_db

    engine, conn, db_path = _engine_and_conn()
    try:
        row_id = _insert(
            conn,
            {
                "service_name": "Ollama",
                "pct_used": 0.0,
                "health": "critical",
                "detail": "⚠ Collection failing — timeout [Cached 346.1m ago]",
            },
        )
        conn.close()
        monkeypatch.setattr("app.core.db.engine", engine)

        init_db()

        with engine.connect() as verify_conn:
            card = _read(verify_conn, row_id)
        assert card["stale"] is True
        assert card["collection_failing"] is True
        assert card["health"] == "good"
    finally:
        engine.dispose()
        if os.path.exists(db_path):
            os.remove(db_path)
