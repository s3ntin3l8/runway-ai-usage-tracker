"""Tests for Grok CLI completed-turn usage extraction from updates.jsonl."""

import json
import os
from datetime import UTC, datetime, timedelta

from scripts.sidecar_pkg.event_extractors.xai import parse_xai_events


def _since(days_ago: int = 1) -> datetime:
    return datetime.now(UTC) - timedelta(days=days_ago)


def _write_updates(tmp_path, records, *, session_id="session-1", cwd="%2Fhome%2Fuser%2Fproj"):
    path = tmp_path / cwd / session_id / "updates.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
    return path


def _envelope(ts, update, *, session_id="session-1"):
    return {
        "timestamp": ts,
        "method": "session/update",
        "params": {"sessionId": session_id, "update": update},
    }


def _turn(prompt_id, usage, *, elapsed_ms=1200):
    return {
        "sessionUpdate": "turn_completed",
        "prompt_id": prompt_id,
        "stop_reason": "end_turn",
        "usage": usage,
        "elapsed_ms": elapsed_ms,
    }


def test_extracts_model_splits_without_double_counting_cache_or_reasoning(tmp_path):
    path = _write_updates(
        tmp_path,
        [
            _envelope(
                "2026-09-25T12:00:00Z",
                _turn(
                    "prompt-1",
                    {
                        "inputTokens": 140,
                        "outputTokens": 50,
                        "cachedReadTokens": 40,
                        "cacheCreationTokens": 10,
                        "reasoningTokens": 20,
                        "modelUsage": {
                            "grok-4": {
                                "inputTokens": 100,
                                "outputTokens": 30,
                                "cachedReadTokens": 25,
                                "cacheCreationTokens": 5,
                                "reasoningTokens": 10,
                                "costUsdTicks": 2_000_000_000,
                            },
                            "grok-4-mini": {
                                "inputTokens": 40,
                                "outputTokens": 20,
                                "cachedReadTokens": 15,
                                "cacheCreationTokens": 5,
                                "reasoningTokens": 10,
                                "costUsdTicks": 500_000_000,
                            },
                        },
                    },
                ),
            )
        ],
    )

    events = parse_xai_events([path], account_id="alice@example.com", since=_since(7))

    assert [
        (
            e.model_id,
            e.tokens_input,
            e.tokens_cache_read,
            e.tokens_cache_create,
            e.tokens_output,
            e.tokens_reasoning,
            e.cost_usd,
        )
        for e in events
    ] == [
        ("grok-4", 70, 25, 5, 20, 10, 0.2),
        ("grok-4-mini", 20, 15, 5, 10, 10, 0.05),
    ]
    assert {e.event_id for e in events} == {
        "xai|grok|session-1|prompt-1|grok-4",
        "xai|grok|session-1|prompt-1|grok-4-mini",
    }
    assert all(e.cwd == "/home/user/proj" for e in events)
    assert all(e.latency_ms == 1200 for e in events)


def test_fallback_uses_current_model_and_turn_totals_only_once(tmp_path):
    path = _write_updates(
        tmp_path,
        [
            _envelope(
                "2026-09-25T12:00:00Z", {"sessionUpdate": "model_changed", "model_id": "grok-4"}
            ),
            _envelope(
                "2026-09-25T12:00:01Z",
                _turn(
                    "p1",
                    {
                        "inputTokens": 100,
                        "cachedReadTokens": 20,
                        "outputTokens": 35,
                        "reasoningTokens": 5,
                        "costUsdTicks": 1_000_000_000,
                    },
                ),
            ),
            _envelope(
                "2026-09-25T12:00:02Z", {"sessionUpdate": "model_changed", "model_id": "grok-4.1"}
            ),
            _envelope(
                "2026-09-25T12:00:03Z",
                _turn(
                    "p2",
                    {
                        "inputTokens": 10,
                        "outputTokens": 4,
                        "usageIsIncomplete": True,
                        "costUsdTicks": 900_000_000,
                    },
                ),
            ),
        ],
    )

    events = parse_xai_events([path], account_id="account", since=_since(7))

    assert [
        (
            e.model_id,
            e.event_id,
            e.tokens_input,
            e.tokens_cache_read,
            e.tokens_output,
            e.tokens_reasoning,
            e.cost_usd,
        )
        for e in events
    ] == [
        ("grok-4", "xai|grok|session-1|p1|grok-4", 80, 20, 30, 5, 0.1),
        ("grok-4.1", "xai|grok|session-1|p2|grok-4.1", 10, 0, 4, 0, None),
    ]


def test_ignores_malformed_non_turn_and_incomplete_records(tmp_path):
    path = _write_updates(
        tmp_path,
        [
            {
                "timestamp": "2026-09-25T12:00:00Z",
                "params": {"update": {"sessionUpdate": "agent_message_chunk"}},
            },
            _envelope("2026-09-25T12:00:01Z", _turn("no-usage", None)),
            _envelope(
                "2026-09-25T12:00:02Z",
                {"sessionUpdate": "turn_completed", "usage": {"inputTokens": 1}},
            ),
            _envelope("2026-09-25T12:00:03Z", _turn("no-token-usage", {"usageIsIncomplete": True})),
        ]
        + [],
    )
    # A malformed line in the middle must not invalidate neighboring entries.
    path.write_text(path.read_text() + "\n{not valid json}\n", encoding="utf-8")

    assert parse_xai_events([path], account_id="a", since=_since(7)) == []


def test_partial_model_cost_is_not_reported(tmp_path):
    path = _write_updates(
        tmp_path,
        [
            _envelope(
                "2026-09-25T12:00:00Z",
                _turn(
                    "p1",
                    {
                        "inputTokens": 15,
                        "outputTokens": 8,
                        "modelUsage": {
                            "grok-4": {
                                "inputTokens": 15,
                                "outputTokens": 8,
                                "costUsdTicks": 5_000_000_000,
                                "costIsPartial": True,
                            }
                        },
                    },
                ),
            )
        ],
    )

    events = parse_xai_events([path], account_id="a", since=_since(7))

    assert len(events) == 1
    assert events[0].tokens_input == 15
    assert events[0].tokens_output == 8
    assert events[0].cost_usd is None


def test_headless_usage_keeps_uncached_input_and_cache_buckets_disjoint(tmp_path):
    path = _write_updates(
        tmp_path,
        [
            _envelope(
                "2026-09-25T12:00:00Z",
                _turn(
                    "p1",
                    {
                        "inputTokens": 60,
                        "cacheReadInputTokens": 40,
                        "cacheCreationInputTokens": 10,
                        "outputTokens": 30,
                        "reasoningTokens": 5,
                    },
                ),
            )
        ],
    )

    events = parse_xai_events([path], account_id="a", since=_since(7))

    assert len(events) == 1
    assert events[0].tokens_input == 60
    assert events[0].tokens_cache_read == 40
    assert events[0].tokens_cache_create == 10
    assert events[0].tokens_output == 25
    assert events[0].tokens_reasoning == 5


def test_headless_and_float_cost_spellings_are_preserved_only_when_complete(tmp_path):
    path = _write_updates(
        tmp_path,
        [
            _envelope("2026-09-25T12:00:00Z", _turn("float-cost", {"costUSD": 1.25})),
            _envelope(
                "2026-09-25T12:00:01Z",
                _turn("headless-ticks", {"total_cost_usd_ticks": 20_000_000_000}),
            ),
            _envelope(
                "2026-09-25T12:00:02Z",
                _turn("headless-float", {"total_cost_usd": 3.5}),
            ),
            _envelope(
                "2026-09-25T12:00:03Z",
                _turn(
                    "incomplete-headless",
                    {
                        "input_tokens": 1,
                        "total_cost_usd_ticks": 90_000_000_000,
                        "usage_is_incomplete": True,
                    },
                ),
            ),
            _envelope(
                "2026-09-25T12:00:04Z",
                _turn(
                    "partial-headless-int",
                    {"input_tokens": 1, "total_cost_usd": 90.0, "cost_is_partial": 1},
                ),
            ),
            _envelope(
                "2026-09-25T12:00:05Z",
                _turn(
                    "partial-headless-string",
                    {"input_tokens": 1, "total_cost_usd": 90.0, "cost_is_partial": "true"},
                ),
            ),
            _envelope(
                "2026-09-25T12:00:06Z",
                _turn(
                    "incomplete-headless-string",
                    {"input_tokens": 1, "total_cost_usd": 90.0, "usage_is_incomplete": "true"},
                ),
            ),
        ],
    )

    events = parse_xai_events([path], account_id="a", since=_since(7))

    assert [(event.event_id, event.cost_usd) for event in events] == [
        ("xai|grok|session-1|float-cost|unknown", 1.25),
        ("xai|grok|session-1|headless-ticks|unknown", 2.0),
        ("xai|grok|session-1|headless-float|unknown", 3.5),
        ("xai|grok|session-1|incomplete-headless|unknown", None),
        ("xai|grok|session-1|partial-headless-int|unknown", None),
        ("xai|grok|session-1|partial-headless-string|unknown", None),
        ("xai|grok|session-1|incomplete-headless-string|unknown", None),
    ]


def test_legacy_direct_notification_uses_file_mtime_fallback(tmp_path):
    fallback_ts = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    path = _write_updates(
        tmp_path,
        [
            {
                "sessionId": "legacy-session",
                "update": _turn("legacy-prompt", {"inputTokens": 12}),
            }
        ],
    )
    path_timestamp = fallback_ts.timestamp()
    os.utime(path, (path_timestamp, path_timestamp))

    events = parse_xai_events([path], account_id="a", since=fallback_ts - timedelta(seconds=2))

    assert len(events) == 1
    assert events[0].event_id == "xai|grok|legacy-session|legacy-prompt|unknown"
    assert events[0].ts == fallback_ts.isoformat()


def test_numeric_epoch_timestamp_seconds_and_milliseconds(tmp_path):
    base = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    epoch_seconds = base.timestamp()
    timestamps = [
        int(epoch_seconds),
        int(epoch_seconds * 1000),
        epoch_seconds + 0.25,
        epoch_seconds * 1000 + 250,
    ]
    path = _write_updates(
        tmp_path,
        [
            _envelope(timestamp, _turn(f"p{index}", {"inputTokens": 1}))
            for index, timestamp in enumerate(timestamps)
        ],
    )

    events = parse_xai_events([path], account_id="a", since=base - timedelta(days=1))

    assert [event.ts for event in events] == [
        base.isoformat(),
        base.isoformat(),
        (base + timedelta(milliseconds=250)).isoformat(),
        (base + timedelta(milliseconds=250)).isoformat(),
    ]


def test_same_second_turns_survive_watermark_overlap_and_replay_with_stable_ids(tmp_path):
    timestamp = "2026-09-25T12:00:00Z"
    path = _write_updates(
        tmp_path,
        [
            _envelope(timestamp, _turn("p1", {"inputTokens": 10})),
            _envelope(timestamp, _turn("p2", {"inputTokens": 20})),
        ],
    )
    since = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)

    first = parse_xai_events([path], account_id="a", since=since)
    replay = parse_xai_events([path], account_id="a", since=since)

    assert [e.event_id for e in first] == [
        "xai|grok|session-1|p1|unknown",
        "xai|grok|session-1|p2|unknown",
    ]
    assert [e.event_id for e in replay] == [e.event_id for e in first]
    assert [e.ts for e in first] == ["2026-09-25T12:00:00+00:00"] * 2


def test_configured_bootstrap_timestamp_window_controls_old_turns(tmp_path):
    path = _write_updates(
        tmp_path,
        [
            _envelope("2026-07-27T12:00:00Z", _turn("inside-90d", {"inputTokens": 1})),
            _envelope("2026-06-25T12:00:00Z", _turn("outside-90d", {"inputTokens": 1})),
        ],
    )
    now = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)

    events = parse_xai_events([path], account_id="a", since=now - timedelta(days=90))

    assert [event.event_id for event in events] == ["xai|grok|session-1|inside-90d|unknown"]


def test_missing_file_is_ignored(tmp_path):
    assert (
        parse_xai_events([tmp_path / "missing" / "updates.jsonl"], account_id="a", since=_since())
        == []
    )
