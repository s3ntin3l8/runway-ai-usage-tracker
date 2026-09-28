"""Unit tests for the Hermes Agent event extractor."""

import json
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
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("HERMES_HOME", raising=False)

    hermes_dir = fake_home / ".hermes"
    hermes_dir.mkdir()
    default_db = hermes_dir / "state.db"
    default_db.touch()

    profiles_dir = hermes_dir / "profiles"
    review_bot_dir = profiles_dir / "review-bot"
    review_bot_dir.mkdir(parents=True)
    review_db = review_bot_dir / "state.db"
    review_db.touch()

    paths = _discover_hermes_db_paths()
    assert default_db in paths
    assert review_db in paths
    assert len(paths) == 2


def test_discover_hermes_db_paths_with_hermes_home_profiles(monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    custom_dir = tmp_path / "custom_hermes"
    custom_dir.mkdir()
    custom_default_db = custom_dir / "state.db"
    custom_default_db.touch()

    custom_profiles = custom_dir / "profiles" / "worker"
    custom_profiles.mkdir(parents=True)
    worker_db = custom_profiles / "state.db"
    worker_db.touch()

    monkeypatch.setenv("HERMES_HOME", str(custom_dir))

    paths = _discover_hermes_db_paths()
    assert custom_default_db in paths
    assert worker_db in paths
    assert len(paths) == 2


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
        assert kimi_ev.account_source == "default"
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
        assert mm_ev.account_id == "default"
        assert mm_ev.account_source == "default"
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
        assert kimi_ev.account_source == "tag"

        mm_ev = next(e for e in events if e.model_id == "MiniMax-M3")
        assert mm_ev.account_id == "operator@example.com"
        assert mm_ev.account_source == "tag"
    finally:
        db_path.unlink(missing_ok=True)


def test_unhinted_canonical_provider_holds_back_as_default_even_with_custom_host_account(
    tmp_path,
):
    """When an event maps to a canonical provider (e.g. kimi_coding or minimax),
    the host's Hermes account label (e.g. 'bot-team') does NOT prove which
    upstream provider account owns the message. It must be held back as
    account_id='default' and account_source='default' for operator assignment.
    """
    db_path, _ = _make_db()
    state_file = tmp_path / "hermes_watermark.json"

    try:
        events = parse_hermes_events(
            db_paths=[db_path],
            account_id="bot-team",
            since=datetime(2020, 1, 1, tzinfo=UTC),
            state_file=state_file,
            canonical_hints=None,
        )

        kimi_ev = next(e for e in events if e.model_id == "kimi-for-coding")
        assert kimi_ev.provider_id == "kimi_coding"
        assert kimi_ev.account_id == "default"
        assert kimi_ev.account_source == "default"

        mm_ev = next(e for e in events if e.model_id == "MiniMax-M3")
        assert mm_ev.provider_id == "minimax"
        assert mm_ev.account_id == "default"
        assert mm_ev.account_source == "default"
    finally:
        db_path.unlink(missing_ok=True)


def test_native_provider_preserves_host_account_id_and_leaves_account_source_none(tmp_path):
    """Native non-canonical Hermes events retain the host account_id and leave
    account_source=None so sidecar.py can stamp its local/tag attribution.
    """
    conn = make_hermes_db(":memory:")
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO sessions (
            id, source, profile_name, model, billing_provider, billing_base_url,
            cwd, git_branch, started_at, ended_at, input_tokens, output_tokens,
            estimated_cost_usd, actual_cost_usd
        ) VALUES (
            'sess-native-01', 'api_server', 'default', 'local-llm',
            'custom-internal', 'http://localhost:8000', '/home/bjoern',
            'main', 1780003000.0, 1780003500.0, 100, 50, 0.0, 0.0
        )
    """)
    cur.execute("""
        INSERT INTO session_model_usage (
            session_id, model, billing_provider, billing_base_url, billing_mode,
            task, api_call_count, input_tokens, output_tokens, cache_read_tokens,
            cache_write_tokens, reasoning_tokens, estimated_cost_usd, actual_cost_usd,
            cost_status, cost_source, first_seen, last_seen
        ) VALUES (
            'sess-native-01', 'local-llm', 'custom-internal',
            'http://localhost:8000', '', '', 1, 100, 50, 0,
            0, 0, 0.0, 0.0, 'none', 'none', 1780003000.0, 1780003500.0
        )
    """)
    conn.commit()

    db_path = tmp_path / "native_state.db"
    file_conn = sqlite3.connect(str(db_path))
    conn.backup(file_conn)
    file_conn.close()
    conn.close()

    state_file = tmp_path / "hermes_watermark.json"

    try:
        events = parse_hermes_events(
            db_paths=[db_path],
            account_id="bot-team",
            since=datetime(2020, 1, 1, tzinfo=UTC),
            state_file=state_file,
        )
        native_ev = next(e for e in events if e.model_id == "local-llm")
        assert native_ev.provider_id == "hermes-custom-internal"
        assert native_ev.account_id == "bot-team"
        assert native_ev.account_source is None
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
        assert delta_ev.event_id.endswith("|c11s2")
    finally:
        db_path.unlink(missing_ok=True)


def test_watermark_counters_decrease_resilience(tmp_path):
    """If session_model_usage counters decrease (e.g. session rollback or edit),
    the watermark must not regress. When counters later exceed the previous
    high-water mark, only the genuine net-new delta is emitted without double-counting.
    """
    db_path, _ = _make_db()
    state_file = tmp_path / "hermes_watermark.json"

    try:
        # Initial run: Kimi has 50,000 input tokens, 10 calls
        first_events = parse_hermes_events(
            db_paths=[db_path],
            account_id="default",
            since=datetime(2020, 1, 1, tzinfo=UTC),
            state_file=state_file,
        )
        assert len(first_events) == 2

        # Simulate a counter decrease / rollback in the SQLite database
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            UPDATE session_model_usage
            SET input_tokens = 40000,
                api_call_count = 8
            WHERE session_id = 'api-sess-kimi-01'
        """)
        conn.commit()
        conn.close()

        # Run again: should NOT emit events and should NOT regress watermark
        second_events = parse_hermes_events(
            db_paths=[db_path],
            account_id="default",
            since=datetime(2020, 1, 1, tzinfo=UTC),
            state_file=state_file,
        )
        assert len(second_events) == 0

        # Simulate recovery past the previous high-water mark: 52,000 tokens (was 50,000), 11 calls
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            UPDATE session_model_usage
            SET input_tokens = 52000,
                api_call_count = 11,
                last_seen = 1780000700.0
            WHERE session_id = 'api-sess-kimi-01'
        """)
        conn.commit()
        conn.close()

        # Third run: delta must be computed against the 50,000 baseline (+2000), NOT 40,000 (+12000)
        third_events = parse_hermes_events(
            db_paths=[db_path],
            account_id="default",
            since=datetime(2020, 1, 1, tzinfo=UTC),
            state_file=state_file,
        )
        assert len(third_events) == 1
        delta_ev = third_events[0]
        assert delta_ev.provider_id == "kimi_coding"
        assert delta_ev.tokens_input == 2000
        assert delta_ev.event_id.endswith("|c11s2")
    finally:
        db_path.unlink(missing_ok=True)


def test_late_cost_correction_emits_distinct_event_id(tmp_path):
    """When a late cost update arrives with identical tokens and api_call_count,
    it must emit a new event with the cost delta and a unique event_id that
    won't be deduped by the server.
    """
    db_path, _ = _make_db()
    state_file = tmp_path / "hermes_watermark.json"

    try:
        # Initial run: MiniMax has 80,000 input, 5 calls, $0.05 cost
        first_events = parse_hermes_events(
            db_paths=[db_path],
            account_id="default",
            since=datetime(2020, 1, 1, tzinfo=UTC),
            state_file=state_file,
        )
        mm_ev1 = next(e for e in first_events if e.model_id == "MiniMax-M3")
        assert mm_ev1.cost_usd == pytest.approx(0.05)
        assert mm_ev1.event_id.endswith("|c5s1")

        # Simulate late cost correction from provider invoice: $0.05 -> $0.08
        # Call count and token counts remain unchanged.
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            UPDATE session_model_usage
            SET actual_cost_usd = 0.08,
                last_seen = 1780001600.0
            WHERE session_id = 'api-sess-minimax-02'
        """)
        conn.commit()
        conn.close()

        # Second run: should emit only the $0.03 cost delta with distinct event_id
        second_events = parse_hermes_events(
            db_paths=[db_path],
            account_id="default",
            since=datetime(2020, 1, 1, tzinfo=UTC),
            state_file=state_file,
        )
        assert len(second_events) == 1
        mm_ev2 = second_events[0]
        assert mm_ev2.provider_id == "minimax"
        assert mm_ev2.cost_usd == pytest.approx(0.03)
        assert mm_ev2.tokens_input == 0
        assert mm_ev2.event_id.endswith("|c5s2")
        assert mm_ev2.event_id != mm_ev1.event_id

        # Third run without changes: 0 events
        third_events = parse_hermes_events(
            db_paths=[db_path],
            account_id="default",
            since=datetime(2020, 1, 1, tzinfo=UTC),
            state_file=state_file,
        )
        assert len(third_events) == 0
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


def test_distinct_db_roots_have_independent_watermarks(tmp_path):
    """If two distinct root directories each contain a state.db with identical
    session_id and profile_name, resolving db_path prevents their slice watermark
    state from colliding."""
    dir1 = tmp_path / "root1"
    dir2 = tmp_path / "root2"
    dir1.mkdir()
    dir2.mkdir()

    db1, _ = _make_db()
    db2, _ = _make_db()
    target1 = dir1 / "state.db"
    target2 = dir2 / "state.db"
    db1.rename(target1)
    db2.rename(target2)

    state_file = tmp_path / "hermes_watermark.json"

    events1 = parse_hermes_events(
        [target1], "default", datetime(2020, 1, 1, tzinfo=UTC), state_file=state_file
    )
    assert len(events1) == 2

    # Second root has identical session IDs, but distinct resolved path, so it must also emit its events
    events2 = parse_hermes_events(
        [target2], "default", datetime(2020, 1, 1, tzinfo=UTC), state_file=state_file
    )
    assert len(events2) == 2

    # And watermark file contains distinct keys for both resolved paths
    data = json.loads(state_file.read_text(encoding="utf-8"))
    resolved_keys = list(data.keys())
    assert any(str(target1.resolve()) in k for k in resolved_keys)
    assert any(str(target2.resolve()) in k for k in resolved_keys)


def test_missing_both_timestamps_skips_and_warns(tmp_path, caplog):
    """Rows with both last_seen and first_seen NULL are skipped with a warning."""
    import logging

    conn = make_hermes_db(":memory:")
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO sessions (
            id, source, profile_name, model, billing_provider, billing_base_url,
            cwd, git_branch, started_at, ended_at, input_tokens, output_tokens,
            estimated_cost_usd, actual_cost_usd
        ) VALUES (
            'sess-null-ts', 'api_server', 'default', 'local-llm',
            'custom', 'http://localhost:8000', '/home', 'main',
            1780003000.0, 1780003500.0, 100, 50, 0.0, 0.0
        )
    """)
    cur.execute("""
        INSERT INTO session_model_usage (
            session_id, model, billing_provider, billing_base_url, billing_mode,
            task, api_call_count, input_tokens, output_tokens, cache_read_tokens,
            cache_write_tokens, reasoning_tokens, estimated_cost_usd, actual_cost_usd,
            cost_status, cost_source, first_seen, last_seen
        ) VALUES (
            'sess-null-ts', 'local-llm', 'custom', 'http://localhost:8000', '',
            '', 1, 100, 50, 0, 0, 0, 0.0, 0.0, 'none', 'none', NULL, NULL
        )
    """)
    conn.commit()

    db_path = tmp_path / "null_ts.db"
    file_conn = sqlite3.connect(str(db_path))
    conn.backup(file_conn)
    file_conn.close()
    conn.close()

    with caplog.at_level(logging.WARNING):
        events = parse_hermes_events(
            db_paths=[db_path],
            account_id="default",
            since=datetime(2020, 1, 1, tzinfo=UTC),
        )
    assert not any(e.session_id == "sess-null-ts" for e in events)
    assert any("has both last_seen and first_seen NULL" in r.message for r in caplog.records)


def test_watermark_mixed_counter_regression_preserves_high_watermark(tmp_path):
    """If a new turn occurs (api_call_count increases), but input_tokens in the DB
    is lower than the prior high-water mark (e.g. session truncation / partial context reset),
    max(curr_in, prev_in) prevents input_tokens from regressing to the lower value.
    """
    db_path, _ = _make_db()
    state_file = tmp_path / "hermes_watermark.json"

    try:
        # Run 1: baseline Kimi has 50,000 input, 10 calls
        first_events = parse_hermes_events(
            [db_path], "default", datetime(2020, 1, 1, tzinfo=UTC), state_file=state_file
        )
        assert len(first_events) == 2

        # Run 2: api_call_count increases 10 -> 11, but input_tokens dropped to 45,000
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            UPDATE session_model_usage
            SET input_tokens = 45000,
                api_call_count = 11,
                last_seen = 1780000750.0
            WHERE session_id = 'api-sess-kimi-01'
        """)
        conn.commit()
        conn.close()

        second_events = parse_hermes_events(
            [db_path], "default", datetime(2020, 1, 1, tzinfo=UTC), state_file=state_file
        )
        assert len(second_events) == 1
        assert second_events[0].tokens_input == 0

        # Verify that state_file retained 50,000 as high-water mark via max(), NOT 45,000
        data = json.loads(state_file.read_text(encoding="utf-8"))
        kimi_slice = next(v for k, v in data.items() if "kimi-for-coding" in k)
        assert kimi_slice["input_tokens"] == 50000
        assert kimi_slice["api_call_count"] == 11
    finally:
        db_path.unlink(missing_ok=True)


def test_discover_hermes_db_paths_canonicalizes_symlinks(tmp_path, monkeypatch):
    """If HERMES_HOME points to a symlink of ~/.hermes, discovery canonicalizes with
    resolve() and returns the database only once."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    real_dir = fake_home / ".hermes"
    real_dir.mkdir()
    real_db = real_dir / "state.db"
    real_db.touch()

    symlink_dir = tmp_path / "symlink_hermes"
    symlink_dir.symlink_to(real_dir)

    monkeypatch.setenv("HERMES_HOME", str(symlink_dir))

    discovered = _discover_hermes_db_paths()
    assert len(discovered) == 1
    assert discovered[0].resolve() == real_db.resolve()


def test_hermes_entrypoint_distinguishes_source(tmp_path):
    """Sessions with non-api_server source (e.g. discord, cron) propagate
    as hermes-<source> entrypoint."""
    conn = make_hermes_db(":memory:")
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO sessions (
            id, source, profile_name, model, billing_provider, billing_base_url,
            cwd, git_branch, started_at, ended_at, input_tokens, output_tokens,
            estimated_cost_usd, actual_cost_usd
        ) VALUES (
            'sess-discord-01', 'discord', 'default', 'local-llm',
            'custom', 'http://localhost:8000', '/home', 'main',
            1780003000.0, 1780003500.0, 100, 50, 0.0, 0.0
        )
    """)
    cur.execute("""
        INSERT INTO session_model_usage (
            session_id, model, billing_provider, billing_base_url, billing_mode,
            task, api_call_count, input_tokens, output_tokens, cache_read_tokens,
            cache_write_tokens, reasoning_tokens, estimated_cost_usd, actual_cost_usd,
            cost_status, cost_source, first_seen, last_seen
        ) VALUES (
            'sess-discord-01', 'local-llm', 'custom', 'http://localhost:8000', '',
            '', 1, 100, 50, 0, 0, 0, 0.0, 0.0, 'none', 'none', 1780003000.0, 1780003500.0
        )
    """)
    conn.commit()

    db_path = tmp_path / "discord_state.db"
    file_conn = sqlite3.connect(str(db_path))
    conn.backup(file_conn)
    file_conn.close()
    conn.close()

    events = parse_hermes_events([db_path], "default", datetime(2020, 1, 1, tzinfo=UTC))
    discord_ev = next(e for e in events if e.session_id == "sess-discord-01")
    assert discord_ev.entrypoint == "hermes-discord"

    # Default api_server sessions have entrypoint="hermes"
    api_ev = next(e for e in events if e.session_id == "api-sess-kimi-01")
    assert api_ev.entrypoint == "hermes"
