"""Unit tests for the Hermes Agent event extractor."""

import json
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.sidecar_pkg.event_extractors.hermes import (
    WATERMARK_PRUNE_IDLE_DAYS,
    _discover_hermes_db_paths,
    _prune_stale_watermark_slices,
    map_hermes_canonical,
    map_hermes_provider_id,
    parse_hermes_events,
    resolve_hermes_provider_and_canonical,
)
from tests.fixtures.hermes_fixture import make_hermes_db


def _make_db(directory: Path) -> tuple[Path, sqlite3.Connection]:
    """Create a SQLite file in ``directory`` with the Hermes schema and sample data."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", dir=directory, delete=False)
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
    assert map_hermes_canonical("kimi") == ("kimi_coding", None)
    assert map_hermes_canonical("minimax-oauth") == ("minimax", None)
    assert map_hermes_canonical("minimax") == ("minimax", None)
    assert map_hermes_canonical("opencode-go") == ("opencode", None)
    assert map_hermes_canonical("opencode-zen") == ("opencode", None)
    assert map_hermes_canonical("opencode-free") == ("opencode-free", None)
    assert map_hermes_canonical("openrouter") == ("openrouter", None)
    assert map_hermes_canonical("deepseek") == ("deepseek", None)
    assert map_hermes_canonical("xai") == ("xai", None)
    assert map_hermes_canonical("xai-oauth") == ("xai", None)
    assert map_hermes_canonical("unknown-provider") is None


def test_provider_id_mapping():
    assert map_hermes_provider_id("kimi-coding") == "kimi_coding"
    assert map_hermes_provider_id("minimax-oauth") == "minimax"
    assert map_hermes_provider_id("xai-oauth") == "xai"
    assert map_hermes_provider_id("custom-llm") == "hermes-custom-llm"
    assert map_hermes_provider_id("") == "hermes"


def test_resolve_hermes_provider_and_canonical():
    # xai-oauth maps to xai
    pid, canon = resolve_hermes_provider_and_canonical(
        "xai-oauth", "https://api.x.ai/v1", "grok-4.7"
    )
    assert pid == "xai"
    assert canon == ("xai", None)

    # Empty billing_provider with kimi model resolves to kimi_coding
    pid, canon = resolve_hermes_provider_and_canonical("", "", "kimi-for-coding")
    assert pid == "kimi_coding"
    assert canon == ("kimi_coding", None)

    # Empty billing_provider falls back to parent session opencode-go
    pid, canon = resolve_hermes_provider_and_canonical(
        "", "", "deepseek-v4-flash", session_billing_provider="opencode-go"
    )
    assert pid == "opencode"
    assert canon == ("opencode", None)

    # Empty billing_provider with free model falls back to opencode-free
    pid, canon = resolve_hermes_provider_and_canonical(
        "", "", "deepseek-v4-flash-free", session_billing_provider="opencode-go"
    )
    assert pid == "opencode-free"
    assert canon == ("opencode-free", None)

    # auto billing_provider with opencode base_url
    pid, canon = resolve_hermes_provider_and_canonical(
        "auto", "https://opencode.ai/zen/v1/", "deepseek-v4-flash"
    )
    assert pid == "opencode"
    assert canon == ("opencode", None)

    pid, canon = resolve_hermes_provider_and_canonical(
        "auto", "https://opencode.ai/zen/v1/", "deepseek-v4-flash-free"
    )
    assert pid == "opencode-free"
    assert canon == ("opencode-free", None)

    # Colon free model syntax
    pid, canon = resolve_hermes_provider_and_canonical(
        "auto", "https://opencode.ai/zen/v1/", "nvidia/nemotron-3-ultra-550b-a55b:free"
    )
    assert pid == "opencode-free"
    assert canon == ("opencode-free", None)

    # grok model with empty billing_provider resolves to xai
    pid, canon = resolve_hermes_provider_and_canonical("", "", "grok-4.7")
    assert pid == "xai"
    assert canon == ("xai", None)

    # False-positive heuristic guard: model with -free- inside name is NOT free tier
    pid, canon = resolve_hermes_provider_and_canonical("opencode", "", "claude-free-preview")
    assert pid == "opencode"
    assert canon == ("opencode", None)

    # Host matching: exact host or subdomain matches
    pid, canon = resolve_hermes_provider_and_canonical("", "https://api.kimi.com/v1", "")
    assert pid == "kimi_coding"
    assert canon == ("kimi_coding", None)

    pid, canon = resolve_hermes_provider_and_canonical("", "https://api.minimax.io/v1", "")
    assert pid == "minimax"
    assert canon == ("minimax", None)

    # Substring attacker domain is rejected
    pid, _ = resolve_hermes_provider_and_canonical("", "https://evil-kimi.com/v1", "")
    assert pid == "hermes"

    # MiniMax model with empty billing_provider resolves to minimax
    pid, canon = resolve_hermes_provider_and_canonical("", "", "MiniMax-M3")
    assert pid == "minimax"
    assert canon == ("minimax", None)

    # MiniMax greedy prefix rejection: non-hyphenated model is rejected
    pid, _ = resolve_hermes_provider_and_canonical("", "", "minimaximal")
    assert pid == "hermes"

    # session_billing_mode="free" routes opencode to opencode-free even for paid model name
    pid, canon = resolve_hermes_provider_and_canonical(
        "opencode", "", "claude-3-5-sonnet", session_billing_mode="free"
    )
    assert pid == "opencode-free"
    assert canon == ("opencode-free", None)


# ---------------------------------------------------------------------------
# Path Discovery Tests
# ---------------------------------------------------------------------------


def test_discover_hermes_db_paths(monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
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
    monkeypatch.setenv("USERPROFILE", str(fake_home))

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
    db_path, _ = _make_db(tmp_path)
    state_file = tmp_path / "hermes_watermark.json"

    try:
        events = parse_hermes_events(
            db_paths=[db_path],
            account_id="default",
            since=datetime(2020, 1, 1, tzinfo=UTC),
            state_file=state_file,
        )

        assert len(events) == 2
        # The client app is Hermes even for events retagged onto another
        # provider (kimi_coding below) — the provider says who was billed,
        # source_app says who made the call.
        assert all(e.source_app == "hermes" for e in events)

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
    db_path, _ = _make_db(tmp_path)
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
    db_path, _ = _make_db(tmp_path)
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
    db_path, _ = _make_db(tmp_path)
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
        # Event ID ends with |c<curr_calls>s<emission_seq> (|c11s2 = 11 calls, 2nd emission slice)
        assert delta_ev.event_id.endswith("|c11s2")
    finally:
        db_path.unlink(missing_ok=True)


def test_watermark_counters_decrease_resilience(tmp_path):
    """If session_model_usage counters decrease (e.g. session rollback or edit),
    the watermark must not regress. When counters later exceed the previous
    high-water mark, only the genuine net-new delta is emitted without double-counting.
    """
    db_path, _ = _make_db(tmp_path)
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
    db_path, _ = _make_db(tmp_path)
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

    db1, _ = _make_db(tmp_path)
    db2, _ = _make_db(tmp_path)
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
            state_file=tmp_path / "hermes_watermark.json",
        )
    assert not any(e.session_id == "sess-null-ts" for e in events)
    assert any("has both last_seen and first_seen NULL" in r.message for r in caplog.records)


def test_watermark_mixed_counter_regression_preserves_high_watermark(tmp_path):
    """If a new turn occurs (api_call_count increases), but input_tokens in the DB
    is lower than the prior high-water mark (e.g. session truncation / partial context reset),
    max(curr_in, prev_in) prevents input_tokens from regressing to the lower value.
    """
    db_path, _ = _make_db(tmp_path)
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


def test_discover_hermes_db_paths_canonicalizes_symlinks(tmp_path, monkeypatch, make_symlink):
    """If HERMES_HOME points to a symlink of ~/.hermes, discovery canonicalizes with
    resolve() and returns the database only once."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))

    real_dir = fake_home / ".hermes"
    real_dir.mkdir()
    real_db = real_dir / "state.db"
    real_db.touch()

    symlink_dir = tmp_path / "symlink_hermes"
    make_symlink(symlink_dir, real_dir)

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

    events = parse_hermes_events(
        [db_path],
        "default",
        datetime(2020, 1, 1, tzinfo=UTC),
        state_file=tmp_path / "hermes_watermark.json",
    )
    discord_ev = next(e for e in events if e.session_id == "sess-discord-01")
    assert discord_ev.entrypoint == "hermes-discord"

    # Default api_server sessions have entrypoint="hermes"
    api_ev = next(e for e in events if e.session_id == "api-sess-kimi-01")
    assert api_ev.entrypoint == "hermes"


def test_gemini_model_ids_are_normalized_to_pricing_buckets(tmp_path):
    """Gemini events carry the versioned bucket (flash-2.5), not the raw id,
    so the cost calculator finds a pricing row; event_id keeps the raw model."""
    conn = make_hermes_db(":memory:")
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO sessions (
            id, source, profile_name, model, billing_provider, billing_base_url,
            cwd, git_branch, started_at, ended_at, input_tokens, output_tokens,
            estimated_cost_usd, actual_cost_usd
        ) VALUES (
            'sess-gem-01', 'api_server', 'default', 'gemini-2.5-flash',
            'gemini', 'https://generativelanguage.googleapis.com', '/home', 'main',
            1780004000.0, 1780004500.0, 100, 50, 0.0, 0.0
        )
    """)
    cur.execute("""
        INSERT INTO session_model_usage (
            session_id, model, billing_provider, billing_base_url, billing_mode,
            task, api_call_count, input_tokens, output_tokens, cache_read_tokens,
            cache_write_tokens, reasoning_tokens, estimated_cost_usd, actual_cost_usd,
            cost_status, cost_source, first_seen, last_seen
        ) VALUES (
            'sess-gem-01', 'gemini-2.5-flash', 'gemini',
            'https://generativelanguage.googleapis.com', '',
            '', 1, 100, 50, 0, 0, 0, 0.0, 0.0, 'none', 'none', 1780004000.0, 1780004500.0
        )
    """)
    conn.commit()

    db_path = tmp_path / "gemini_state.db"
    file_conn = sqlite3.connect(str(db_path))
    conn.backup(file_conn)
    file_conn.close()
    conn.close()

    events = parse_hermes_events(
        [db_path],
        "default",
        datetime(2020, 1, 1, tzinfo=UTC),
        state_file=tmp_path / "hermes_watermark.json",
    )
    gem_ev = next(e for e in events if e.session_id == "sess-gem-01")
    assert gem_ev.provider_id == "gemini"
    assert gem_ev.model_id == "flash-2.5"
    assert "|gemini-2.5-flash|" in gem_ev.event_id


def test_watermark_state_key_scoped_by_account_id(tmp_path):
    """Watermark state keys are scoped by account_id so multiple accounts
    iterating through scoped_accounts do not advance each other's watermarks."""
    db_path, _ = _make_db(tmp_path)
    state_file = tmp_path / "hermes_watermark.json"

    events1 = parse_hermes_events(
        [db_path], "account1", datetime(2020, 1, 1, tzinfo=UTC), state_file=state_file
    )
    assert len(events1) == 2

    # Second account run against same DB still emits events because its watermark is independent
    events2 = parse_hermes_events(
        [db_path], "account2", datetime(2020, 1, 1, tzinfo=UTC), state_file=state_file
    )
    assert len(events2) == 2

    data = json.loads(state_file.read_text(encoding="utf-8"))
    keys = list(data.keys())
    assert any(k.startswith("account1|") for k in keys)
    assert any(k.startswith("account2|") for k in keys)


def test_watermark_state_key_distinguishes_billing_base_url_and_mode(tmp_path):
    """Watermark state keys include billing_base_url and billing_mode to prevent
    collision when a session produces multiple slices with different endpoints or modes."""
    db_path = tmp_path / "state.db"
    conn = make_hermes_db(str(db_path))

    # Add a second row for the same session and model but differing in billing_base_url
    conn.execute("""
        INSERT INTO session_model_usage (
            session_id, model, billing_provider, billing_base_url, billing_mode,
            task, api_call_count, input_tokens, output_tokens, cache_read_tokens,
            cache_write_tokens, reasoning_tokens, estimated_cost_usd, actual_cost_usd,
            cost_status, cost_source, first_seen, last_seen
        ) VALUES (
            'api-sess-kimi-01', 'kimi-for-coding', 'kimi-coding',
            'https://api-backup.kimi.com/coding/v1/', '', '', 5, 25000, 1000, 0,
            0, 0, 0.0, 0.0, 'unknown', 'none', 1780000100.0, 1780000600.0
        )
    """)
    conn.commit()
    conn.close()

    state_file = tmp_path / "hermes_watermark.json"
    try:
        events = parse_hermes_events(
            [db_path], "default", datetime(2020, 1, 1, tzinfo=UTC), state_file=state_file
        )
        # 2 original rows + 1 new row = 3 events
        assert len(events) == 3

        data = json.loads(state_file.read_text(encoding="utf-8"))
        # Both distinct base URLs exist as independent watermark keys
        url1_keys = [k for k in data if "https://api.kimi.com/coding/v1/" in k]
        url2_keys = [k for k in data if "https://api-backup.kimi.com/coding/v1/" in k]
        assert len(url1_keys) == 1
        assert len(url2_keys) == 1
    finally:
        db_path.unlink(missing_ok=True)


def test_first_seen_newer_than_last_seen_extracted(tmp_path):
    """Rows where first_seen > since_epoch are extracted even if last_seen is older or zero."""
    db_path = tmp_path / "state.db"
    conn = make_hermes_db(str(db_path))

    # Add a row where last_seen is 0.0 (or older) but first_seen is 1790000000.0 (> since)
    conn.execute("""
        INSERT INTO session_model_usage (
            session_id, model, billing_provider, billing_base_url, billing_mode,
            task, api_call_count, input_tokens, output_tokens, cache_read_tokens,
            cache_write_tokens, reasoning_tokens, estimated_cost_usd, actual_cost_usd,
            cost_status, cost_source, first_seen, last_seen
        ) VALUES (
            'api-sess-kimi-01', 'custom-model', 'custom',
            '', '', '', 1, 500, 100, 0, 0, 0, 0.0, 0.0, 'none', 'none', 1790000000.0, 1770000000.0
        )
    """)
    conn.commit()
    conn.close()

    try:
        events = parse_hermes_events(
            [db_path], "default", datetime.fromtimestamp(1785000000.0, tz=UTC)
        )
        assert len(events) == 1
        assert events[0].model_id == "custom-model"
    finally:
        db_path.unlink(missing_ok=True)


def test_hermes_parent_session_and_model_heuristics_extraction(tmp_path):
    """Auxiliary tasks with empty or auto billing_provider resolve via parent session and model heuristics."""
    db_path = tmp_path / "state.db"
    conn = make_hermes_db(str(db_path))

    # 1. Parent session is opencode-go; title_generation task has empty billing_provider
    conn.execute("""
        INSERT INTO sessions (
            id, source, profile_name, model, billing_provider, billing_base_url,
            started_at, ended_at
        ) VALUES (
            'sess-opencode-01', 'api_server', 'review-bot', 'deepseek-v4-flash',
            'opencode-go', 'https://opencode.ai/zen/go/v1', 1780000000.0, 1780000500.0
        )
    """)
    conn.execute("""
        INSERT INTO session_model_usage (
            session_id, model, billing_provider, billing_base_url, billing_mode,
            task, api_call_count, input_tokens, output_tokens, cache_read_tokens,
            cache_write_tokens, reasoning_tokens, estimated_cost_usd, actual_cost_usd,
            cost_status, cost_source, first_seen, last_seen
        ) VALUES (
            'sess-opencode-01', 'deepseek-v4-flash', '', '', '',
            'title_generation', 1, 500, 50, 0, 0, 0, 0.0, 0.0, 'none', 'none',
            1780000100.0, 1780000500.0
        )
    """)

    # 2. xai-oauth billing_provider
    conn.execute("""
        INSERT INTO sessions (
            id, source, profile_name, model, billing_provider, billing_base_url,
            started_at, ended_at
        ) VALUES (
            'sess-xai-01', 'api_server', 'review-bot', 'grok-4.7',
            'xai-oauth', 'https://api.x.ai/v1', 1780000000.0, 1780000500.0
        )
    """)
    conn.execute("""
        INSERT INTO session_model_usage (
            session_id, model, billing_provider, billing_base_url, billing_mode,
            task, api_call_count, input_tokens, output_tokens, cache_read_tokens,
            cache_write_tokens, reasoning_tokens, estimated_cost_usd, actual_cost_usd,
            cost_status, cost_source, first_seen, last_seen
        ) VALUES (
            'sess-xai-01', 'grok-4.7', 'xai-oauth', 'https://api.x.ai/v1', '',
            'main', 5, 2000, 300, 0, 0, 0, 0.0, 0.0, 'none', 'none',
            1780000100.0, 1780000500.0
        )
    """)

    # 3. auto billing_provider with free model against opencode.ai URL
    conn.execute("""
        INSERT INTO sessions (
            id, source, profile_name, model, billing_provider, billing_base_url,
            started_at, ended_at
        ) VALUES (
            'sess-free-01', 'api_server', 'review-bot', 'deepseek-v4-flash-free',
            'opencode-zen', 'https://opencode.ai/zen/v1', 1780000000.0, 1780000500.0
        )
    """)
    conn.execute("""
        INSERT INTO session_model_usage (
            session_id, model, billing_provider, billing_base_url, billing_mode,
            task, api_call_count, input_tokens, output_tokens, cache_read_tokens,
            cache_write_tokens, reasoning_tokens, estimated_cost_usd, actual_cost_usd,
            cost_status, cost_source, first_seen, last_seen
        ) VALUES (
            'sess-free-01', 'deepseek-v4-flash-free', 'auto', 'https://opencode.ai/zen/v1/', '',
            'compression', 2, 800, 100, 0, 0, 0, 0.0, 0.0, 'none', 'none',
            1780000100.0, 1780000500.0
        )
    """)

    # 4. Kimi model in session whose main billing provider was minimax
    conn.execute("""
        INSERT INTO session_model_usage (
            session_id, model, billing_provider, billing_base_url, billing_mode,
            task, api_call_count, input_tokens, output_tokens, cache_read_tokens,
            cache_write_tokens, reasoning_tokens, estimated_cost_usd, actual_cost_usd,
            cost_status, cost_source, first_seen, last_seen
        ) VALUES (
            'api-sess-minimax-02', 'kimi-for-coding', '', '', '',
            'title_generation', 1, 592, 23, 0, 0, 0, 0.0, 0.0, 'none', 'none',
            1780001000.0, 1780001500.0
        )
    """)

    conn.commit()
    conn.close()

    canonical_hints = {
        "opencode": {"provider:opencode": "operator@opencode.com"},
        "xai": {"provider:xai": "operator@xai.com"},
    }

    try:
        events = parse_hermes_events(
            [db_path],
            "fallback@host.com",
            datetime.fromtimestamp(1779000000.0, tz=UTC),
            canonical_hints=canonical_hints,
            state_file=tmp_path / "hermes_watermark.json",
        )

        by_session_task = {(ev.session_id, ev.subagent_type): ev for ev in events}

        # 1. Opencode title generation resolved from session to opencode
        ev_oc = by_session_task[("sess-opencode-01", "title_generation")]
        assert ev_oc.provider_id == "opencode"
        assert ev_oc.account_id == "operator@opencode.com"
        assert ev_oc.account_source == "tag"

        # 2. xai-oauth resolved to xai
        ev_xai = by_session_task[("sess-xai-01", "main")]
        assert ev_xai.provider_id == "xai"
        assert ev_xai.account_id == "operator@xai.com"
        assert ev_xai.account_source == "tag"

        # 3. auto with free model resolved to opencode-free and inherited provider:opencode hint
        ev_free = by_session_task[("sess-free-01", "compression")]
        assert ev_free.provider_id == "opencode-free"
        assert ev_free.account_id == "operator@opencode.com"
        assert ev_free.account_source == "tag"

        # 4. Kimi model resolved to kimi_coding even when session was minimax
        ev_kimi = by_session_task[("api-sess-minimax-02", "title_generation")]
        assert ev_kimi.provider_id == "kimi_coding"
        # No hint for kimi_coding was provided -> holds back as default
        assert ev_kimi.account_id == "default"
        assert ev_kimi.account_source == "default"
    finally:
        db_path.unlink(missing_ok=True)


def test_the_default_watermark_path_matches_the_sidecar_config_dir(monkeypatch, tmp_path):
    """The extractor's fallback path must not drift from the sidecar's.

    The extractor cannot import ``sidecar.get_sidecar_dir()`` (the frozen app
    runs ``scripts/sidecar.py`` as a script), so the two are pinned together
    here, including the Windows ``%APPDATA%`` branch.
    """
    import scripts.sidecar as sidecar
    from scripts.sidecar_pkg.event_extractors import hermes

    def assert_in_step() -> None:
        assert hermes._default_watermark_state_path() == (
            sidecar.get_sidecar_dir() / "hermes_watermark.json"
        )

    monkeypatch.setenv("RUNWAY_CONFIG_DIR", str(tmp_path))
    assert_in_step()
    monkeypatch.delenv("RUNWAY_CONFIG_DIR")
    assert_in_step()

    monkeypatch.setattr("platform.system", lambda: "Windows")
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    assert_in_step()
    assert hermes._default_watermark_state_path() == (
        tmp_path / "roaming" / "runway" / "sidecar" / "hermes_watermark.json"
    )
    monkeypatch.delenv("APPDATA")
    assert_in_step()


# ---------------------------------------------------------------------------
# Watermark Compaction Tests (#926)
# ---------------------------------------------------------------------------


def _set_row_times(db_path: Path, *, last_seen: float) -> None:
    """Move every usage row's timestamps to ``last_seen``."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "UPDATE session_model_usage SET last_seen = ?, first_seen = ?",
        (last_seen, last_seen),
    )
    conn.commit()
    conn.close()


def _cutoff(seconds: float = 30 * 86400) -> float:
    """The idle cutoff a prune uses, pinned by the test rather than by the
    implementation's constant: a test that derives its expectation from
    ``WATERMARK_PRUNE_IDLE_DAYS`` moves with it and cannot catch a guard that
    was weakened.
    """
    return datetime.now(UTC).timestamp() - seconds


def _watermark_keys(state_file: Path) -> list[str]:
    if not state_file.exists():
        return []
    return list(json.loads(state_file.read_text(encoding="utf-8")).keys())


def test_prune_drops_the_slices_of_a_vanished_database(tmp_path):
    """A database that is gone leaves its high-water marks behind for good."""
    db_path, _ = _make_db(tmp_path)
    state_file = tmp_path / "hermes_watermark.json"

    try:
        parse_hermes_events(
            db_paths=[db_path],
            account_id="default",
            since=datetime(2020, 1, 1, tzinfo=UTC),
            state_file=state_file,
        )
        assert len(_watermark_keys(state_file)) == 2

        # The database is deleted: its slices are now dead weight.
        db_path.unlink()
        state = json.loads(state_file.read_text(encoding="utf-8"))
        pruned = _prune_stale_watermark_slices(
            state, [], account_id="default", idle_cutoff=_cutoff()
        )
        assert pruned == 2
        assert state == {}
    finally:
        db_path.unlink(missing_ok=True)


def test_prune_keeps_a_recently_used_slice_of_a_vanished_database(tmp_path):
    """The database may only be *temporarily* gone (a backup renamed it aside).

    A session that was active recently keeps its mark, so the sidecar still
    knows what it already reported when the file comes back.
    """
    db_path, _ = _make_db(tmp_path)
    state_file = tmp_path / "hermes_watermark.json"
    now = datetime.now(UTC).timestamp()

    try:
        _set_row_times(db_path, last_seen=now)
        parse_hermes_events(
            db_paths=[db_path],
            account_id="default",
            since=datetime(2020, 1, 1, tzinfo=UTC),
            state_file=state_file,
        )
        assert len(_watermark_keys(state_file)) == 2

        db_path.unlink()
        state = json.loads(state_file.read_text(encoding="utf-8"))
        pruned = _prune_stale_watermark_slices(
            state, [], account_id="default", idle_cutoff=_cutoff()
        )
        assert pruned == 0
        assert len(state) == 2
    finally:
        db_path.unlink(missing_ok=True)


def test_prune_keeps_the_slices_of_a_live_database(tmp_path):
    """A database that exists keeps every slice, however old the session is."""
    db_path, _ = _make_db(tmp_path)
    state_file = tmp_path / "hermes_watermark.json"

    try:
        parse_hermes_events(
            db_paths=[db_path],
            account_id="default",
            since=datetime(2020, 1, 1, tzinfo=UTC),
            state_file=state_file,
        )
        state = json.loads(state_file.read_text(encoding="utf-8"))
        pruned = _prune_stale_watermark_slices(
            state, [db_path], account_id="default", idle_cutoff=_cutoff()
        )
        assert pruned == 0
        assert len(state) == 2
    finally:
        db_path.unlink(missing_ok=True)


def test_prune_leaves_a_key_it_cannot_parse_alone(tmp_path):
    """A key we cannot read the path out of is never pruned."""
    state_file = tmp_path / "hermes_watermark.json"
    state_file.write_text(
        json.dumps({"default|not-a-path|default|sess|model|hermes|||": {"last_seen": 0.0}}),
        encoding="utf-8",
    )

    state = json.loads(state_file.read_text(encoding="utf-8"))
    pruned = _prune_stale_watermark_slices(
        state,
        [],
        account_id="default",
        idle_cutoff=_cutoff(),
    )
    assert pruned == 0
    assert len(state) == 1


def test_prune_skips_a_path_that_is_only_the_prefix_of_a_live_path(tmp_path):
    """A database whose own path contains a ``|`` is cut short by the split.

    The truncated form is not on disk, but it is a prefix of the real path, so
    pruning it would drop the mark of a database that is right there.
    """
    db_path = tmp_path / "pipe|name.db"
    conn = make_hermes_db(str(db_path))
    conn.close()
    state_file = tmp_path / "hermes_watermark.json"

    try:
        parse_hermes_events(
            db_paths=[db_path],
            account_id="default",
            since=datetime(2020, 1, 1, tzinfo=UTC),
            state_file=state_file,
        )
        keys = _watermark_keys(state_file)
        assert len(keys) == 2
        assert any("|" in key.split("|", 2)[1] or "pipe" in key for key in keys)

        # The file is gone, but the discovered path still names it.
        db_path.unlink()
        state = json.loads(state_file.read_text(encoding="utf-8"))
        pruned = _prune_stale_watermark_slices(
            state, [db_path], account_id="default", idle_cutoff=_cutoff()
        )
        assert pruned == 0
        assert len(state) == 2
    finally:
        db_path.unlink(missing_ok=True)


def test_prune_is_written_out_even_when_the_cycle_emits_nothing(tmp_path):
    """A cycle that reports nothing can still shrink the file, and it must."""
    db_path, _ = _make_db(tmp_path)
    state_file = tmp_path / "hermes_watermark.json"

    try:
        parse_hermes_events(
            db_paths=[db_path],
            account_id="default",
            since=datetime(2020, 1, 1, tzinfo=UTC),
            state_file=state_file,
        )
        assert len(_watermark_keys(state_file)) == 2

        db_path.unlink()
        events = parse_hermes_events(
            db_paths=[],
            account_id="default",
            since=datetime(2020, 1, 1, tzinfo=UTC),
            state_file=state_file,
        )
        assert events == []
        assert _watermark_keys(state_file) == []
    finally:
        db_path.unlink(missing_ok=True)


def test_a_reappearing_database_does_not_double_count(tmp_path):
    """A database that comes back must report the delta, not its whole life.

    This is the regression test for the idle guard: a prune that dropped the
    mark of a recently used session would make the sidecar report the
    session's entire lifetime as new usage, under an event id the server has
    never seen (the emission counter restarts), and the server would count it
    twice.
    """
    db_path, _ = _make_db(tmp_path)
    state_file = tmp_path / "hermes_watermark.json"
    now = datetime.now(UTC).timestamp()

    try:
        _set_row_times(db_path, last_seen=now)
        first = parse_hermes_events(
            db_paths=[db_path],
            account_id="default",
            since=datetime(2020, 1, 1, tzinfo=UTC),
            state_file=state_file,
        )
        assert len(first) == 2

        # The file disappears (a backup renamed it aside) and the sidecar
        # reports nothing while it is gone.
        db_path.unlink()
        parse_hermes_events(
            db_paths=[],
            account_id="default",
            since=datetime(2020, 1, 1, tzinfo=UTC),
            state_file=state_file,
        )
        assert len(_watermark_keys(state_file)) == 2

        # It comes back and the session moved on.
        conn = make_hermes_db(str(db_path))
        conn.execute(
            """
            UPDATE session_model_usage
            SET input_tokens = input_tokens + 5000,
                output_tokens = output_tokens + 300,
                api_call_count = 11,
                first_seen = ?,
                last_seen = ?
            WHERE session_id = 'api-sess-kimi-01'
            """,
            (now, now),
        )
        conn.commit()
        conn.close()

        again = parse_hermes_events(
            db_paths=[db_path],
            account_id="default",
            since=datetime(2020, 1, 1, tzinfo=UTC),
            state_file=state_file,
        )
        assert len(again) == 1
        delta = again[0]
        assert delta.session_id == "api-sess-kimi-01"
        # The delta, not the session's whole lifetime (50000 + 5000).
        assert delta.tokens_input == 5000
        assert delta.tokens_output == 300
        # The emission counter continues, so the event id stays one the server
        # has already seen for this slice.
        assert delta.event_id.endswith("|c11s2")
    finally:
        db_path.unlink(missing_ok=True)


def test_prune_reads_the_path_of_an_account_id_that_holds_a_pipe(tmp_path):
    """The account segment is stripped by its exact prefix, not by position.

    An account id may be an opaque string, and one that holds a ``|`` would
    shift every field after it. Stripping the known prefix keeps the path
    readable instead of pointing at a fragment of the account id.
    """
    db_path, _ = _make_db(tmp_path)
    state_file = tmp_path / "hermes_watermark.json"
    account = "operator|tag"

    try:
        parse_hermes_events(
            db_paths=[db_path],
            account_id=account,
            since=datetime(2020, 1, 1, tzinfo=UTC),
            state_file=state_file,
        )
        keys = _watermark_keys(state_file)
        assert len(keys) == 2
        assert all(key.startswith(f"{account}|") for key in keys)

        # The path is read correctly, so the live database keeps its slices.
        state = json.loads(state_file.read_text(encoding="utf-8"))
        pruned = _prune_stale_watermark_slices(
            state, [db_path], account_id=account, idle_cutoff=_cutoff()
        )
        assert pruned == 0
        assert len(state) == 2

        # And once the file is gone, the slices are reclaimed.
        db_path.unlink()
        state = json.loads(state_file.read_text(encoding="utf-8"))
        pruned = _prune_stale_watermark_slices(state, [], account_id=account, idle_cutoff=_cutoff())
        assert pruned == 2
        assert state == {}
    finally:
        db_path.unlink(missing_ok=True)


def test_prune_applies_the_documented_idle_window(tmp_path):
    """The grace window the extractor documents is the one it applies.

    The boundary is checked through the real parse path, so the window the
    implementation uses is pinned rather than the constant alone.
    """
    now = datetime.now(UTC).timestamp()
    window = WATERMARK_PRUNE_IDLE_DAYS * 86400

    def _keys_after_prune(idle_seconds: float) -> list[str]:
        """Write a slice idle for ``idle_seconds``, drop its file, prune."""
        state_file = tmp_path / f"hermes_watermark_{int(idle_seconds)}.json"
        db_path, _ = _make_db(tmp_path)
        try:
            _set_row_times(db_path, last_seen=now - idle_seconds)
            parse_hermes_events(
                db_paths=[db_path],
                account_id="default",
                since=datetime(2020, 1, 1, tzinfo=UTC),
                state_file=state_file,
            )
            assert len(_watermark_keys(state_file)) == 2
            db_path.unlink()
            parse_hermes_events(
                db_paths=[],
                account_id="default",
                since=datetime(2020, 1, 1, tzinfo=UTC),
                state_file=state_file,
            )
            return _watermark_keys(state_file)
        finally:
            db_path.unlink(missing_ok=True)

    # Quiet for less than the window: the mark stays.
    assert len(_keys_after_prune(window - 3600)) == 2
    # Quiet for longer than the window: it is reclaimed.
    assert _keys_after_prune(window + 3600) == []
