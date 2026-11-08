"""Unit tests for Hermes Agent incremental watermark tracking, scoping, and deltas."""

import sqlite3
from datetime import UTC, datetime

import pytest

from scripts.sidecar_pkg.event_extractors.hermes import parse_hermes_events
from tests.fixtures.hermes_fixture import make_hermes_db
from tests.unit._event_extractor_hermes_helpers import _make_db, _watermark_entries


def test_incremental_deltas_watermark(tmp_path):
    db_path, _ = _make_db(tmp_path)
    state_file = tmp_path / "hermes_watermark.jsonl"

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
    state_file = tmp_path / "hermes_watermark.jsonl"

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
    state_file = tmp_path / "hermes_watermark.jsonl"

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

    state_file = tmp_path / "hermes_watermark.jsonl"

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
    data = _watermark_entries(state_file)
    resolved_keys = list(data.keys())
    assert any(str(target1.resolve()) in k for k in resolved_keys)
    assert any(str(target2.resolve()) in k for k in resolved_keys)


def test_watermark_mixed_counter_regression_preserves_high_watermark(tmp_path):
    """If a new turn occurs (api_call_count increases), but input_tokens in the DB
    is lower than the prior high-water mark (e.g. session truncation / partial context reset),
    max(curr_in, prev_in) prevents input_tokens from regressing to the lower value.
    """
    db_path, _ = _make_db(tmp_path)
    state_file = tmp_path / "hermes_watermark.jsonl"

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
        data = _watermark_entries(state_file)
        kimi_slice = next(v for k, v in data.items() if "kimi-for-coding" in k)
        assert kimi_slice["input_tokens"] == 50000
        assert kimi_slice["api_call_count"] == 11
    finally:
        db_path.unlink(missing_ok=True)


def test_watermark_state_key_scoped_by_account_id(tmp_path):
    """Watermark state keys are scoped by account_id so multiple accounts
    iterating through scoped_accounts do not advance each other's watermarks."""
    db_path, _ = _make_db(tmp_path)
    state_file = tmp_path / "hermes_watermark.jsonl"

    events1 = parse_hermes_events(
        [db_path], "account1", datetime(2020, 1, 1, tzinfo=UTC), state_file=state_file
    )
    assert len(events1) == 2

    # Second account run against same DB still emits events because its watermark is independent
    events2 = parse_hermes_events(
        [db_path], "account2", datetime(2020, 1, 1, tzinfo=UTC), state_file=state_file
    )
    assert len(events2) == 2

    data = _watermark_entries(state_file)
    keys = list(data.keys())
    assert any("|account1|" in k for k in keys)
    assert any("|account2|" in k for k in keys)


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

    state_file = tmp_path / "hermes_watermark.jsonl"
    try:
        events = parse_hermes_events(
            [db_path], "default", datetime(2020, 1, 1, tzinfo=UTC), state_file=state_file
        )
        # 2 original rows + 1 new row = 3 events
        assert len(events) == 3

        data = _watermark_entries(state_file)
        # Both distinct base URLs exist as independent watermark keys
        url1_keys = [k for k in data if "https://api.kimi.com/coding/v1/" in k]
        url2_keys = [k for k in data if "https://api-backup.kimi.com/coding/v1/" in k]
        assert len(url1_keys) == 1
        assert len(url2_keys) == 1
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
            sidecar.get_sidecar_dir() / "hermes_watermark.jsonl"
        )

    monkeypatch.setenv("RUNWAY_CONFIG_DIR", str(tmp_path))
    assert_in_step()
    monkeypatch.delenv("RUNWAY_CONFIG_DIR")
    assert_in_step()

    monkeypatch.setattr("platform.system", lambda: "Windows")
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    assert_in_step()
    assert hermes._default_watermark_state_path() == (
        tmp_path / "roaming" / "runway" / "sidecar" / "hermes_watermark.jsonl"
    )
    monkeypatch.delenv("APPDATA")
    assert_in_step()
