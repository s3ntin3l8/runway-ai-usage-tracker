"""Unit tests for the MiniMax Code event extractor."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts.sidecar_pkg.event_extractors import minimax_code
from scripts.sidecar_pkg.event_extractors.minimax_code import (
    commit_pending_state,
    discard_pending_state,
    discover_runtime_store,
    parse_minimax_code_events,
)
from tests.fixtures.minimax_code_fixture import (
    DEFAULT_MESSAGES,
    DEFAULT_USAGE,
    make_minimax_code_store,
    write_session_history,
)

_TS = DEFAULT_USAGE[0]["ts"]
_SINCE = datetime.fromtimestamp(_TS / 1000, tz=UTC) - timedelta(days=1)


@pytest.fixture(autouse=True)
def _reset_pending_advances():
    """Keep the module-level pending-cursor registry out of other tests."""
    minimax_code._PENDING_ADVANCES.clear()
    yield
    minimax_code._PENDING_ADVANCES.clear()


def _store(tmp_path: Path, **kwargs) -> Path:
    return make_minimax_code_store(tmp_path, **kwargs)


def _parse(db_path: Path, tmp_path: Path, *, since: datetime | None = None, account="default"):
    return parse_minimax_code_events(
        db_path,
        account_id=account,
        since=since or _SINCE,
        state_file=tmp_path / "minimax_code_watermark.json",
    )


# --- shape -----------------------------------------------------------------


def test_emits_one_event_per_api_request(tmp_path):
    db_path = _store(tmp_path)
    events = _parse(db_path, tmp_path)
    assert len(events) == len(DEFAULT_USAGE)


def test_events_carry_the_minimax_provider_and_source_app(tmp_path):
    db_path = _store(tmp_path)
    events = _parse(db_path, tmp_path)
    assert events
    for event in events:
        assert event.provider_id == "minimax"
        assert event.source_app == "minimax_code"
        assert event.account_id == "default"
        assert event.kind == "message"


def test_token_values_are_pushed_per_request_as_recorded(tmp_path):
    db_path = _store(tmp_path)
    events = _parse(db_path, tmp_path)
    first = events[0]
    assert first.tokens_input == 16474
    assert first.tokens_output == 203
    assert first.tokens_cache_read == 1536
    assert first.tokens_cache_create == 0
    assert first.tokens_reasoning == 0


def test_cost_is_left_to_the_server_pricing_table(tmp_path):
    db_path = _store(tmp_path)
    events = _parse(db_path, tmp_path)
    assert all(event.cost_usd is None for event in events)


def test_cwd_and_session_id_come_from_the_session_row(tmp_path):
    db_path = _store(tmp_path)
    events = _parse(db_path, tmp_path)
    main = [e for e in events if e.session_id == "mvs_main"]
    assert main
    assert all(e.cwd == "/home/user/project" for e in main)


def test_tool_names_and_finish_reason_come_from_the_message_row(tmp_path):
    db_path = _store(tmp_path)
    events = _parse(db_path, tmp_path)
    first = events[0]
    assert first.tool_names == ["bash", "read"]
    assert first.tool_calls == 2
    assert first.stop_reason == "toolUse"


def test_effort_comes_from_the_session_thinking_config(tmp_path):
    db_path = _store(tmp_path)
    events = _parse(db_path, tmp_path)
    main = [e for e in events if e.session_id == "mvs_main"]
    assert all(e.effort == "xhigh" for e in main)


# --- internal plan-review turns -------------------------------------------


def test_internal_plan_review_turns_are_tagged_as_plan_subagents(tmp_path):
    db_path = _store(tmp_path)
    events = _parse(db_path, tmp_path)
    plan = [e for e in events if e.subagent_type == "Plan"]
    assert len(plan) == 1
    assert plan[0].tokens_input == 800


def test_main_thread_turns_are_not_tagged(tmp_path):
    db_path = _store(tmp_path)
    events = _parse(db_path, tmp_path)
    main = [e for e in events if e.session_id == "mvs_main" and e.subagent_type is None]
    assert len(main) == 3


# --- model resolution ------------------------------------------------------


def test_model_comes_from_the_message_telemetry(tmp_path):
    db_path = _store(tmp_path)
    events = _parse(db_path, tmp_path)
    assert events[0].model_id == "MiniMax-M3.1-Flash"


def test_model_falls_back_to_the_session_effective_model(tmp_path):
    """A request whose message row has not landed yet still gets a model."""
    db_path = _store(tmp_path)
    events = _parse(db_path, tmp_path)
    unmatched = [e for e in events if e.tokens_input == 500]
    assert unmatched
    # The provider prefix is stripped so the id matches the pricing seed.
    assert unmatched[0].model_id == "MiniMax-M3.1-Flash"


def test_model_falls_back_to_the_session_history_jsonl(tmp_path):
    db_path = _store(tmp_path)
    write_session_history(tmp_path, "2026/10/22/21-00-00-000-session_other", model="MiniMax-M3")
    events = _parse(db_path, tmp_path)
    other = [e for e in events if e.session_id == "mvs_other"]
    assert other
    assert other[0].model_id == "MiniMax-M3"


def test_model_falls_back_to_the_llm_call_manifest(tmp_path):
    db_path = _store(tmp_path)
    write_session_history(
        tmp_path, "2026/10/22/21-00-00-000-session_other", llm_model="MiniMax-M3.1-Flash"
    )
    events = _parse(db_path, tmp_path)
    other = [e for e in events if e.session_id == "mvs_other"]
    assert other
    assert other[0].model_id == "MiniMax-M3.1-Flash"


def test_model_stays_none_when_nothing_names_one(tmp_path):
    db_path = _store(tmp_path)
    events = _parse(db_path, tmp_path)
    other = [e for e in events if e.session_id == "mvs_other"]
    assert other
    assert other[0].model_id is None


# --- the id cursor ---------------------------------------------------------


def test_committed_cursor_stops_the_rows_from_being_re_emitted(tmp_path):
    db_path = _store(tmp_path)
    first = _parse(db_path, tmp_path)
    assert first
    assert commit_pending_state() is True
    assert _parse(db_path, tmp_path) == []


def test_cursor_does_not_move_before_the_ingest_is_confirmed(tmp_path):
    db_path = _store(tmp_path)
    first = _parse(db_path, tmp_path)
    assert first
    state_file = tmp_path / "minimax_code_watermark.json"
    assert not state_file.exists()
    # The rows are still pending, so another pass emits them again — the
    # server deduplicates on (provider_id, event_id).
    second = _parse(db_path, tmp_path)
    assert [e.event_id for e in second] == [e.event_id for e in first]


def test_a_parked_advance_that_was_never_pushed_does_not_skip_rows(tmp_path):
    """A cycle that dies between the extraction and the push must not lose rows.

    The parked advance is not a scan floor: the next pass starts from the
    persisted cursor again, so the rows the dead cycle extracted come back.
    """
    db_path = _store(tmp_path)
    first = _parse(db_path, tmp_path)
    assert first
    # No commit, no discard — the cycle died right here.
    second = _parse(db_path, tmp_path)
    assert [e.event_id for e in second] == [e.event_id for e in first]


def test_failed_ingest_re_emits_the_same_rows(tmp_path):
    db_path = _store(tmp_path)
    first = _parse(db_path, tmp_path)
    assert first
    discard_pending_state()
    second = _parse(db_path, tmp_path)
    assert [e.event_id for e in second] == [e.event_id for e in first]


def test_rebuilt_store_resets_the_cursor_and_re_emits(tmp_path):
    db_path = _store(tmp_path)
    first = _parse(db_path, tmp_path)
    assert first
    commit_pending_state()
    state_file = tmp_path / "minimax_code_watermark.json"
    state = json.loads(state_file.read_text())
    state["databases"][str(db_path.resolve())]["last_id"] = 999999
    state_file.write_text(json.dumps(state))
    discard_pending_state()
    again = _parse(db_path, tmp_path)
    assert [e.event_id for e in again] == [e.event_id for e in first]


def test_event_ids_are_stable_across_runs(tmp_path):
    db_path = _store(tmp_path)
    first = _parse(db_path, tmp_path)
    discard_pending_state()
    second = _parse(db_path, tmp_path)
    assert [e.event_id for e in first] == [e.event_id for e in second]
    assert all(e.event_id.startswith("mcode:") for e in first)


def test_broken_cursor_state_is_ignored(tmp_path):
    db_path = _store(tmp_path)
    state_file = tmp_path / "minimax_code_watermark.json"
    state_file.write_text("{not json")
    events = _parse(db_path, tmp_path)
    assert events


def test_cursor_state_that_is_not_an_object_is_ignored(tmp_path):
    db_path = _store(tmp_path)
    state_file = tmp_path / "minimax_code_watermark.json"
    state_file.write_text("[]")
    events = _parse(db_path, tmp_path)
    assert events


def test_bootstrap_window_that_matches_nothing_skips_the_whole_store(tmp_path):
    db_path = _store(tmp_path)
    since = datetime.fromtimestamp((_TS + 10_000_000) / 1000, tz=UTC)
    assert _parse(db_path, tmp_path, since=since) == []
    # The rows are not rescanned on the next cycle.
    assert _parse(db_path, tmp_path, since=since) == []


def test_request_with_an_unusable_timestamp_is_skipped(tmp_path):
    usage = [dict(row) for row in DEFAULT_USAGE]
    usage[0]["ts"] = 10**15  # far past year 9999
    db_path = _store(tmp_path, usage=usage)
    events = _parse(db_path, tmp_path)
    assert len(events) == len(usage) - 1


def test_two_requests_with_the_same_timestamp_are_both_kept(tmp_path):
    usage = [dict(row) for row in DEFAULT_USAGE]
    clone = dict(usage[0])
    clone["id"] = 99
    usage.append(clone)
    db_path = _store(tmp_path, usage=usage)
    events = _parse(db_path, tmp_path)
    assert len(events) == len(usage)
    assert len({e.event_id for e in events}) == len(events)


def test_message_row_without_a_usage_block_is_not_joined(tmp_path):
    messages = [dict(row) for row in DEFAULT_MESSAGES]
    messages[0]["data_json"] = {"canonical_message_id": "msg-empty"}
    db_path = _store(tmp_path, messages=messages)
    events = _parse(db_path, tmp_path)
    first = events[0]
    # The request still lands — with the session's model — but without the
    # message row's tools.
    assert first.model_id == "MiniMax-M3.1-Flash"
    assert first.tool_names is None
    assert first.stop_reason is None


def test_unparsable_tool_calls_are_ignored(tmp_path):
    messages = [dict(row) for row in DEFAULT_MESSAGES]
    data = dict(messages[0]["data_json"])
    data["tool_calls"] = ["not-a-dict", {"tool_name": "  "}, {"name": "grep"}]
    messages[0]["data_json"] = data
    db_path = _store(tmp_path, messages=messages)
    events = _parse(db_path, tmp_path)
    assert events[0].tool_names == ["grep"]
    assert events[0].tool_calls == 1


def test_bootstrap_window_skips_rows_outside_it(tmp_path):
    db_path = _store(tmp_path)
    since = datetime.fromtimestamp((_TS + 15000) / 1000, tz=UTC)
    events = _parse(db_path, tmp_path, since=since)
    assert len(events) == 1
    assert events[0].session_id == "mvs_other"


def test_a_large_store_is_paged_without_gaps_or_overlap(tmp_path, monkeypatch):
    """The per-cycle cap must not skip or duplicate rows."""
    monkeypatch.setattr(minimax_code, "_MAX_ROWS_PER_CYCLE", 3)
    usage = [
        {
            "id": index + 1,
            "session_id": "mvs_main",
            "turn_id": f"turn_{index}",
            "ts": _TS + index,
            "input_tokens": 100 + index,
            "output_tokens": 10,
            "reasoning_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }
        for index in range(7)
    ]
    db_path = _store(tmp_path, usage=usage, messages=[])

    seen: list[str] = []
    for _ in range(3):
        events = _parse(db_path, tmp_path)
        seen.extend(e.event_id for e in events)
        commit_pending_state()

    assert len(seen) == len(usage)
    assert len(set(seen)) == len(usage)


# --- degradation -----------------------------------------------------------


def test_missing_store_returns_no_events(tmp_path):
    assert _parse(tmp_path / "nope.sqlite", tmp_path) == []


def test_schema_drift_returns_no_events(tmp_path):
    db_path = _store(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute("DROP TABLE local_runtime_message_rows")
    conn.commit()
    conn.close()
    assert _parse(db_path, tmp_path) == []


def test_missing_column_returns_no_events(tmp_path):
    db_path = _store(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute("ALTER TABLE local_runtime_token_usage DROP COLUMN cache_read_tokens")
    conn.commit()
    conn.close()
    assert _parse(db_path, tmp_path) == []


# --- discovery -------------------------------------------------------------


def test_discovery_finds_the_runtime_store(tmp_path, monkeypatch):
    make_minimax_code_store(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("MINIMAX_CODE_HOME", raising=False)
    found = discover_runtime_store()
    assert found is not None
    assert found.name == "runtime-state.sqlite"


def test_discovery_returns_none_when_the_runtime_never_ran(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("MINIMAX_CODE_HOME", raising=False)
    assert discover_runtime_store() is None


def test_discovery_honours_the_home_override(tmp_path, monkeypatch):
    make_minimax_code_store(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "elsewhere"))
    monkeypatch.setenv("MINIMAX_CODE_HOME", str(tmp_path / ".minimax"))
    assert discover_runtime_store() is not None


# --- sidecar wiring --------------------------------------------------------


def _sidecar():
    import scripts.sidecar as sidecar_module

    return sidecar_module


class _MockWatermark:
    def last_pushed(self, provider_id, account_id):
        return None


def test_minimax_is_an_event_provider():
    assert "minimax" in _sidecar()._EVENT_PROVIDERS


def test_minimax_account_identity_is_the_default_sentinel():
    sidecar = _sidecar()
    assert sidecar._minimax_code_account_identity() == "default"
    assert sidecar._LEGACY_EVENT_ACCOUNT_DISCOVERY["minimax"]() == "default"


def test_minimax_account_extractor_wiring(monkeypatch, tmp_path):
    sidecar = _sidecar()
    mock_parser = MagicMock(return_value=["mock_event"])
    extractor = sidecar._make_account_extractor_minimax_code(mock_parser)

    # No store on this host: nothing to extract, and the parser is not called.
    monkeypatch.setattr(sidecar, "_discover_minimax_code_db_path", lambda: None)
    assert extractor("default", _MockWatermark(), 30) == []
    assert not mock_parser.called

    mock_db = tmp_path / "runtime-state.sqlite"
    mock_db.touch()
    monkeypatch.setattr(sidecar, "_discover_minimax_code_db_path", lambda: mock_db)
    events = extractor("default", _MockWatermark(), 30)
    assert events == ["mock_event"]
    args, kwargs = mock_parser.call_args
    assert args[0] == mock_db
    assert kwargs["account_id"] == "default"
    assert kwargs["since"] is not None
    # The cursor lives next to the rest of the sidecar's state.
    assert kwargs["state_file"].name == "minimax_code_watermark.json"
    assert kwargs["state_file"].parent == sidecar.get_sidecar_dir()


def test_minimax_events_are_dispatched_under_the_minimax_provider(monkeypatch, tmp_path):
    sidecar = _sidecar()
    db_path = make_minimax_code_store(tmp_path)
    monkeypatch.setattr(sidecar, "_discover_minimax_code_db_path", lambda: db_path)
    out_events: list[dict] = []
    failures = sidecar._extract_events_for_provider(
        provider_id="minimax",
        account_ids=["default"],
        watermark=_MockWatermark(),
        bootstrap_days=30,
        out_events=out_events,
        account_source="default",
    )
    assert failures == 0
    assert out_events
    assert {event["provider_id"] for event in out_events} == {"minimax"}
    assert {event["source_app"] for event in out_events} == {"minimax_code"}


# --- the cursor across a real sidecar cycle --------------------------------


def _minimax_payloads() -> list[dict]:
    return [
        {
            "provider_id": "minimax",
            "source_app": "minimax_code",
            "account_id": "default",
            "event_id": f"mcode:s:turn:{index}",
            "ts": "2026-10-22T20:12:31.617000+00:00",
        }
        for index in range(2)
    ]


def _run_cycle(tmp_path, *, reply):
    """Run one sidecar cycle with the ingest reply mocked."""
    from unittest.mock import patch

    sidecar = _sidecar()
    state_file = tmp_path / "minimax_code_watermark.json"

    def fake_collection(*args, **kwargs):
        # The extractor parks the cursor advance; the cycle decides whether
        # it is committed.
        minimax_code._PENDING_ADVANCES[str(state_file)] = {"/db/runtime-state.sqlite": 42}
        return sidecar.CollectionResult([], _minimax_payloads(), 0, [])

    runner = sidecar.DaemonRunner(
        {
            "api_url": "http://localhost:8765",
            "api_key": "test-key",  # pragma: allowlist secret
            "retry_attempts": 1,
            "retry_backoff_seconds": 0,
        }
    )
    with (
        patch.object(sidecar, "run_collection", side_effect=fake_collection),
        patch.object(sidecar, "http_post_signed_with_retry", return_value=reply),
        patch.object(sidecar, "queue_flush"),
    ):
        runner.run_once()
    return state_file


def test_cursor_is_committed_after_a_successful_ingest(tmp_path):
    state_file = _run_cycle(tmp_path, reply=(True, {"poll_providers": []}, 200))
    assert state_file.exists()
    state = json.loads(state_file.read_text())
    assert state["databases"]["/db/runtime-state.sqlite"]["last_id"] == 42


def test_cursor_is_not_committed_when_the_ingest_fails(tmp_path):
    state_file = _run_cycle(tmp_path, reply=(True, {"events_error": True}, 200))
    assert not state_file.exists()


def test_cursor_is_not_committed_when_the_push_fails(tmp_path):
    state_file = _run_cycle(tmp_path, reply=(False, "server unavailable", 503))
    assert not state_file.exists()


def test_a_parked_advance_from_a_dead_cycle_does_not_survive(tmp_path):
    """The cycle-start reset drops an advance the dead cycle left behind."""
    sidecar = _sidecar()
    minimax_code._PENDING_ADVANCES["/state.json"] = {"/db": 99}
    sidecar._reset_minimax_cursor_state()
    assert minimax_code._PENDING_ADVANCES == {}


def test_the_default_cursor_path_matches_the_sidecar_config_dir(monkeypatch, tmp_path):
    """The extractor's fallback path must not drift from the sidecar's.

    The extractor cannot import ``sidecar.get_sidecar_dir()`` (the frozen
    app bundles ``scripts/sidecar.py`` as data and runs it as a script),
    so the two are pinned together here instead.
    """
    sidecar = _sidecar()
    monkeypatch.setenv("RUNWAY_CONFIG_DIR", str(tmp_path))
    assert minimax_code._default_state_path() == sidecar.get_sidecar_dir() / (
        "minimax_code_watermark.json"
    )
    monkeypatch.delenv("RUNWAY_CONFIG_DIR")
    assert minimax_code._default_state_path() == sidecar.get_sidecar_dir() / (
        "minimax_code_watermark.json"
    )


def test_a_rebuilt_store_lowers_the_cursor(tmp_path):
    """A store whose id axis restarted must not keep the old cursor.

    The old cursor points past the end of the new table, so keeping it
    would re-emit the whole store on every cycle and never reach the
    rows past the per-cycle cap.
    """
    db_path = _store(tmp_path)
    state_file = tmp_path / "minimax_code_watermark.json"
    state_file.write_text(json.dumps({"databases": {str(db_path.resolve()): {"last_id": 50005}}}))
    discard_pending_state()
    events = _parse(db_path, tmp_path)
    assert events
    assert commit_pending_state() is True
    state = json.loads(state_file.read_text())
    assert state["databases"][str(db_path.resolve())]["last_id"] == max(
        row["id"] for row in DEFAULT_USAGE
    )


def test_same_millisecond_requests_keep_their_tokens_across_the_paging_cap(tmp_path, monkeypatch):
    """A same-millisecond pair split by the row cap must not collapse.

    Both rows carry the same turn and timestamp, so only their usage
    tells them apart — and the id has to say so even when the two land in
    different batches.
    """
    monkeypatch.setattr(minimax_code, "_MAX_ROWS_PER_CYCLE", 1)
    usage = [
        {
            "id": 1,
            "session_id": "mvs_main",
            "turn_id": "turn_main",
            "ts": 1792699951617,
            "input_tokens": 100,
            "output_tokens": 10,
            "reasoning_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        },
        {
            "id": 2,
            "session_id": "mvs_main",
            "turn_id": "turn_main",
            "ts": 1792699951617,
            "input_tokens": 200,
            "output_tokens": 20,
            "reasoning_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        },
    ]
    db_path = _store(tmp_path, usage=usage, messages=[])

    ids: list[str] = []
    tokens: list[int] = []
    for _ in range(3):
        events = _parse(db_path, tmp_path)
        ids.extend(event.event_id for event in events)
        tokens.extend(event.tokens_input for event in events)
        commit_pending_state()

    assert len(set(ids)) == 2
    assert sorted(tokens) == [100, 200]
