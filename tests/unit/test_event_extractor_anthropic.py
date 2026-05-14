from datetime import UTC, datetime
from pathlib import Path

from scripts.sidecar_pkg.event_extractors.anthropic import parse_anthropic_events

FIXTURE = Path(__file__).parent.parent / "fixtures" / "anthropic-sample.jsonl"
SIDECHAIN_FIXTURE = (
    Path(__file__).parent.parent / "fixtures" / "anthropic-sidechain-sample.jsonl"
)


def test_extracts_assistant_messages_only():
    evts = parse_anthropic_events(
        [FIXTURE],
        account_id="u@x",
        since=datetime(2020, 1, 1, tzinfo=UTC),
    )
    assert len(evts) == 2
    assert all(e.provider_id == "anthropic" for e in evts)
    assert {e.model_id for e in evts} == {"sonnet", "opus"}


def test_dedup_event_id_includes_request_id():
    evts = parse_anthropic_events(
        [FIXTURE],
        account_id="u@x",
        since=datetime(2020, 1, 1, tzinfo=UTC),
    )
    assert all("|" in e.event_id for e in evts)


def test_filters_by_since():
    evts = parse_anthropic_events(
        [FIXTURE],
        account_id="u@x",
        since=datetime(2026, 5, 8, 14, 23, 30, tzinfo=UTC),
    )
    assert len(evts) == 1
    assert evts[0].model_id == "opus"


def test_captures_token_dimensions():
    evts = parse_anthropic_events(
        [FIXTURE],
        account_id="u@x",
        since=datetime(2020, 1, 1, tzinfo=UTC),
    )
    opus = next(e for e in evts if e.model_id == "opus")
    assert opus.tokens_input == 300
    assert opus.tokens_output == 100
    assert opus.tokens_cache_read == 500
    assert opus.tokens_cache_create == 1000


def test_counts_tool_calls():
    evts = parse_anthropic_events(
        [FIXTURE],
        account_id="u@x",
        since=datetime(2020, 1, 1, tzinfo=UTC),
    )
    opus = next(e for e in evts if e.model_id == "opus")
    sonnet = next(e for e in evts if e.model_id == "sonnet")
    assert opus.tool_calls == 1
    assert sonnet.tool_calls == 0


def test_session_id_from_log():
    evts = parse_anthropic_events(
        [FIXTURE],
        account_id="u@x",
        since=datetime(2020, 1, 1, tzinfo=UTC),
    )
    assert all(e.session_id == "sess1" for e in evts)


def test_main_thread_event_has_no_subagent_type():
    evts = parse_anthropic_events(
        [SIDECHAIN_FIXTURE],
        account_id="u@x",
        since=datetime(2020, 1, 1, tzinfo=UTC),
    )
    main = next(e for e in evts if e.event_id.startswith("msg_main_1"))
    assert main.subagent_type is None


def test_sidechain_event_captures_attribution_agent():
    evts = parse_anthropic_events(
        [SIDECHAIN_FIXTURE],
        account_id="u@x",
        since=datetime(2020, 1, 1, tzinfo=UTC),
    )
    by_type = {e.subagent_type for e in evts}
    assert by_type == {None, "Explore", "Plan"}
    explore = next(e for e in evts if e.subagent_type == "Explore")
    assert explore.session_id == "parent_sess"
    assert explore.model_id == "opus"
