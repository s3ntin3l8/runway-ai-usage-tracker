from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.sidecar_pkg.event_extractors.anthropic import (
    _normalize_anthropic_model,
    parse_anthropic_events,
)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "anthropic-sample.jsonl"
SIDECHAIN_FIXTURE = Path(__file__).parent.parent / "fixtures" / "anthropic-sidechain-sample.jsonl"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("claude-opus-4-5-20250929", "opus-4.5"),
        ("claude-sonnet-4-5-20250929", "sonnet-4.5"),
        ("claude-opus-4-7", "opus-4.7"),
        ("claude-sonnet-4-6", "sonnet-4.6"),
        ("claude-opus-4-8-20260515", "opus-4.8"),
        ("claude-3-5-sonnet-20241022", "sonnet-3.5"),
        ("claude-3-5-haiku-20241022", "haiku-3.5"),
        ("claude-3-opus-20240229", "opus-3"),
        ("claude-opus-4-20250514", "opus-4"),
        # New families must keep their version, not truncate to the bare family.
        ("claude-fable-5", "fable-5"),
        ("claude-fable-5-20260601", "fable-5"),
        ("fable", "fable"),
        ("opus", "opus"),
        ("", "unknown"),
    ],
)
def test_normalize_preserves_version(raw, expected):
    assert _normalize_anthropic_model(raw) == expected


def test_extracts_assistant_messages_only():
    evts = parse_anthropic_events(
        [FIXTURE],
        account_id="u@x",
        since=datetime(2020, 1, 1, tzinfo=UTC),
    )
    assert len(evts) == 2
    assert all(e.provider_id == "anthropic" for e in evts)
    assert all(e.source_app == "claude_code" for e in evts)
    assert {e.model_id for e in evts} == {"sonnet-4.5", "opus-4.5"}


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
    assert evts[0].model_id == "opus-4.5"


def test_captures_token_dimensions():
    evts = parse_anthropic_events(
        [FIXTURE],
        account_id="u@x",
        since=datetime(2020, 1, 1, tzinfo=UTC),
    )
    opus = next(e for e in evts if e.model_id == "opus-4.5")
    assert opus.tokens_input == 300
    assert opus.tokens_output == 100
    assert opus.tokens_cache_read == 500
    assert opus.tokens_cache_create == 1000


def test_captures_new_dimensions():
    """effort/speed/service_tier/entrypoint/version and the cache-TTL split /
    web-tool counts, previously discarded, are now captured (opus message)."""
    evts = parse_anthropic_events(
        [FIXTURE],
        account_id="u@x",
        since=datetime(2020, 1, 1, tzinfo=UTC),
    )
    opus = next(e for e in evts if e.model_id == "opus-4.5")
    sonnet = next(e for e in evts if e.model_id == "sonnet-4.5")

    assert opus.effort == "high"
    assert opus.entrypoint == "cli"
    assert opus.app_version == "2.1.217"
    assert opus.speed == "standard"
    assert opus.service_tier == "standard"
    assert opus.tokens_cache_create_1h == 700
    assert opus.tokens_cache_create_5m == 300
    assert opus.tokens_cache_create_1h + opus.tokens_cache_create_5m == opus.tokens_cache_create
    assert opus.web_search_requests == 1
    assert opus.web_fetch_requests == 0

    # The sonnet line in the fixture predates these fields (no effort/version/
    # cache_creation/server_tool_use) — everything defaults cleanly.
    assert sonnet.effort is None
    assert sonnet.app_version is None
    assert sonnet.tokens_cache_create_1h == 0
    assert sonnet.tokens_cache_create_5m == 0
    assert sonnet.web_search_requests == 0


def test_counts_tool_calls():
    evts = parse_anthropic_events(
        [FIXTURE],
        account_id="u@x",
        since=datetime(2020, 1, 1, tzinfo=UTC),
    )
    opus = next(e for e in evts if e.model_id == "opus-4.5")
    sonnet = next(e for e in evts if e.model_id == "sonnet-4.5")
    assert opus.tool_calls == 1
    assert sonnet.tool_calls == 0


def test_session_id_from_log():
    evts = parse_anthropic_events(
        [FIXTURE],
        account_id="u@x",
        since=datetime(2020, 1, 1, tzinfo=UTC),
    )
    assert all(e.session_id == "sess1" for e in evts)


def test_captures_cwd_branch_and_tool_names():
    evts = parse_anthropic_events(
        [FIXTURE],
        account_id="u@x",
        since=datetime(2020, 1, 1, tzinfo=UTC),
    )
    opus = next(e for e in evts if e.model_id == "opus-4.5")
    sonnet = next(e for e in evts if e.model_id == "sonnet-4.5")
    assert opus.cwd == "/home/user/repo"
    assert opus.git_branch == "main"
    assert opus.tool_names == ["Read"]  # the opus message has one tool_use block
    # The sonnet message has no tool_use blocks → no tool names.
    assert sonnet.tool_names is None


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
    assert explore.model_id == "opus-4.7"


# --- session names -----------------------------------------------------------

import json  # noqa: E402

from scripts.sidecar_pkg.event_extractors.anthropic import (  # noqa: E402
    parse_anthropic_session_names,
)


def _write_transcript(path: Path, lines: list[dict]) -> None:
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")


def _assistant(session: str) -> dict:
    return {
        "type": "assistant",
        "sessionId": session,
        "timestamp": "2026-01-01T00:00:00Z",
        "message": {"id": "m1", "usage": {}, "content": []},
    }


def test_session_names_keep_the_last_of_each_kind(tmp_path):
    named = tmp_path / "sess-aaa.jsonl"
    _write_transcript(
        named,
        [
            {"type": "agent-name", "agentName": "first-placeholder", "sessionId": "sess-aaa"},
            {"type": "ai-title", "aiTitle": "Title one", "sessionId": "sess-aaa"},
            _assistant("sess-aaa"),
            {"type": "ai-title", "aiTitle": "Title two", "sessionId": "sess-aaa"},
            {"type": "agent-name", "agentName": "renamed-placeholder", "sessionId": "sess-aaa"},
            {"type": "ai-title", "aiTitle": "Title three", "sessionId": "sess-aaa"},
        ],
    )
    plain = tmp_path / "sess-bbb.jsonl"
    _write_transcript(plain, [_assistant("sess-bbb")])

    records, scanned = parse_anthropic_session_names([named, plain])

    assert {(r["session_id"], r["kind"]): r["name"] for r in records} == {
        ("sess-aaa", "agent"): "renamed-placeholder",
        ("sess-aaa", "generated"): "Title three",
    }
    assert {r["provider_id"] for r in records} == {"anthropic"}
    assert {r["source_app"] for r in records} == {"claude_code"}
    assert set(scanned) == {named, plain}


def test_session_names_after_the_last_event_are_found(tmp_path):
    fp = tmp_path / "sess-ccc.jsonl"
    _write_transcript(
        fp,
        [_assistant("sess-ccc"), {"type": "ai-title", "aiTitle": "Late", "sessionId": "sess-ccc"}],
    )
    records, _ = parse_anthropic_session_names([fp])
    assert [r["name"] for r in records] == ["Late"]


def test_session_names_tolerate_junk_and_skip_records_without_a_session(tmp_path):
    fp = tmp_path / "sess-ddd.jsonl"
    fp.write_text(
        'not json "ai-title"\n'
        + json.dumps({"type": "ai-title", "aiTitle": "   ", "sessionId": "sess-ddd"})
        + "\n"
        + json.dumps({"type": "ai-title", "aiTitle": 7, "sessionId": "sess-ddd"})
        + "\n"
        + json.dumps({"type": "user", "text": "mentions agent-name only", "x": "ai-title"})
        + "\n"
        + json.dumps({"type": "agent-name", "agentName": "orphan-placeholder"})
        + "\n"
        + json.dumps(
            {"type": "agent-name", "agentName": "kept-placeholder", "sessionId": "sess-ddd"}
        )
        + "\n",
        encoding="utf-8",
    )
    records, _ = parse_anthropic_session_names([fp, tmp_path / "missing.jsonl"])
    assert [(r["session_id"], r["kind"], r["name"]) for r in records] == [
        ("sess-ddd", "agent", "kept-placeholder")
    ]


def test_session_names_skip_leaves_the_file_unread(tmp_path):
    fp = tmp_path / "sess-eee.jsonl"
    _write_transcript(fp, [{"type": "ai-title", "aiTitle": "Skipped", "sessionId": "sess-eee"}])
    records, scanned = parse_anthropic_session_names([fp], skip=lambda path: True)
    assert records == [] and scanned == {}
