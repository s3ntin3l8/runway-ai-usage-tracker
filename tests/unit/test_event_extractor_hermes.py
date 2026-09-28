"""Unit tests for the Hermes Agent event extractor."""

import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.sidecar_pkg.event_extractors.hermes import (
    _discover_hermes_db_paths,
    map_hermes_canonical,
    map_hermes_provider_id,
    parse_hermes_events,
)
from tests.fixtures.hermes_fixture import make_hermes_db


def _make_db() -> tuple[Path, sqlite3.Connection]:
    """Create a temp SQLite file with the Hermes schema and sample data."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_path = Path(tmp.name)
    conn = make_hermes_db(str(db_path))
    conn.close()
    return db_path, conn


# ---------------------------------------------------------------------------
# Canonical Mapping Tests
# ---------------------------------------------------------------------------


def test_canonical_provider_mapping():
    assert map_hermes_canonical("kimi-coding") == ("kimi_coding", None)
    assert map_hermes_canonical("minimax-oauth") == ("minimax", None)
    assert map_hermes_canonical("minimax") == ("minimax", None)
    assert map_hermes_canonical("opencode-go") == ("opencode", None)
    assert map_hermes_canonical("opencode-zen") == ("opencode", None)
    assert map_hermes_canonical("openrouter") == ("openrouter", None)
    assert map_hermes_canonical("deepseek") == ("deepseek", None)
    assert map_hermes_canonical("unknown-provider") is None


def test_provider_id_mapping():
    assert map_hermes_provider_id("kimi-coding") == "kimi_coding"
    assert map_hermes_provider_id("minimax-oauth") == "minimax"
    assert map_hermes_provider_id("custom-llm") == "hermes-custom-llm"
    assert map_hermes_provider_id("") == "hermes"


# ---------------------------------------------------------------------------
# Path Discovery Tests
# ---------------------------------------------------------------------------


def test_discover_hermes_db_paths(monkeypatch, tmp_path):
    hermes_dir = tmp_path / ".hermes"
    hermes_dir.mkdir()
    default_db = hermes_dir / "state.db"
    default_db.touch()

    profiles_dir = hermes_dir / "profiles"
    review_bot_dir = profiles_dir / "review-bot"
    review_bot_dir.mkdir(parents=True)
    review_db = review_bot_dir / "state.db"
    review_db.touch()

    monkeypatch.setenv("HERMES_HOME", str(hermes_dir))
    monkeypatch.setattr(
        Path, "expanduser", lambda self: tmp_path / self.name if str(self).startswith("~") else self
    )

    paths = _discover_hermes_db_paths()
    assert default_db in paths or any(p.name == "state.db" for p in paths)


# ---------------------------------------------------------------------------
# Extraction Tests
# ---------------------------------------------------------------------------


def test_parse_hermes_events_basic(tmp_path):
    db_path, _ = _make_db()
    state_file = tmp_path / "hermes_watermark.json"

    try:
        events = parse_hermes_events(
            db_paths=[db_path],
            account_id="default",
            since=datetime(2020, 1, 1, tzinfo=UTC),
            state_file=state_file,
        )

        assert len(events) == 2

        # First event: Kimi Coding
        kimi_ev = next(e for e in events if e.model_id == "kimi-for-coding")
        assert kimi_ev.provider_id == "kimi_coding"
        assert kimi_ev.account_id == "default"
        assert kimi_ev.entrypoint == "hermes"
        assert kimi_ev.tokens_input == 50000
        assert kimi_ev.tokens_output == 2000
        assert kimi_ev.tokens_cache_read == 200000
        assert kimi_ev.tokens_reasoning == 500
        assert kimi_ev.cwd == "/home/bjoern/projects/runway"
        assert kimi_ev.git_branch == "feat/review-check"
        assert kimi_ev.cost_usd is None  # 0.0 costs stay None to allow server pricing

        # Second event: MiniMax with background review task
        mm_ev = next(e for e in events if e.model_id == "MiniMax-M3")
        assert mm_ev.provider_id == "minimax"
        assert mm_ev.subagent_type == "background_review"
        assert mm_ev.tokens_input == 80000
        assert mm_ev.tokens_output == 4000
        assert mm_ev.tokens_cache_read == 150000
        assert mm_ev.cost_usd == pytest.approx(0.05)
    finally:
        db_path.unlink(missing_ok=True)


def test_parse_hermes_events_with_canonical_hints(tmp_path):
    db_path, _ = _make_db()
    state_file = tmp_path / "hermes_watermark.json"

    canonical_hints = {
        "minimax": {"provider:minimax": "operator@example.com"},
        "kimi_coding": {"provider:kimi_coding": "kimi-user@example.com"},
    }

    try:
        events = parse_hermes_events(
            db_paths=[db_path],
            account_id="fallback@example.com",
            since=datetime(2020, 1, 1, tzinfo=UTC),
            canonical_hints=canonical_hints,
            state_file=state_file,
        )

        kimi_ev = next(e for e in events if e.model_id == "kimi-for-coding")
        assert kimi_ev.account_id == "kimi-user@example.com"

        mm_ev = next(e for e in events if e.model_id == "MiniMax-M3")
        assert mm_ev.account_id == "operator@example.com"
    finally:
        db_path.unlink(missing_ok=True)


def test_incremental_deltas_watermark(tmp_path):
    db_path, _ = _make_db()
    state_file = tmp_path / "hermes_watermark.json"

    try:
        # First extraction run
        first_events = parse_hermes_events(
            db_paths=[db_path],
            account_id="default",
            since=datetime(2020, 1, 1, tzinfo=UTC),
            state_file=state_file,
        )
        assert len(first_events) == 2

        # Second run without changes -> should return 0 new events
        second_events = parse_hermes_events(
            db_paths=[db_path],
            account_id="default",
            since=datetime(2020, 1, 1, tzinfo=UTC),
            state_file=state_file,
        )
        assert len(second_events) == 0

        # Simulate new turn in Kimi session: +5000 in, +300 out, api_call_count 10 -> 11
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            UPDATE session_model_usage
            SET input_tokens = input_tokens + 5000,
                output_tokens = output_tokens + 300,
                api_call_count = 11,
                last_seen = 1780000600.0
            WHERE session_id = 'api-sess-kimi-01'
        """)
        conn.commit()
        conn.close()

        # Third run -> should extract only the delta!
        third_events = parse_hermes_events(
            db_paths=[db_path],
            account_id="default",
            since=datetime(2020, 1, 1, tzinfo=UTC),
            state_file=state_file,
        )
        assert len(third_events) == 1
        delta_ev = third_events[0]
        assert delta_ev.provider_id == "kimi_coding"
        assert delta_ev.tokens_input == 5000
        assert delta_ev.tokens_output == 300
        assert delta_ev.event_id.endswith("|11")
    finally:
        db_path.unlink(missing_ok=True)


def test_missing_or_corrupt_db(tmp_path):
    missing_path = tmp_path / "nonexistent.db"
    events = parse_hermes_events(
        db_paths=[missing_path],
        account_id="default",
        since=datetime(2020, 1, 1, tzinfo=UTC),
    )
    assert events == []
