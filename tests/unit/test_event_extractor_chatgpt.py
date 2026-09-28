"""Unit tests for the ChatGPT/Codex event extractor."""

import json
from datetime import UTC, datetime
from pathlib import Path

from scripts.sidecar_pkg.event_extractors.chatgpt import (
    _normalize_chatgpt_model,
    parse_chatgpt_events,
)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "chatgpt-sample.jsonl"


# ---------------------------------------------------------------------------
# Model normalisation
# ---------------------------------------------------------------------------


def test_preserves_codex_model():
    assert _normalize_chatgpt_model("gpt-5-codex") == "gpt-5-codex"


def test_normalizes_gpt5_model():
    assert _normalize_chatgpt_model("gpt-5") == "gpt-5"


def test_normalizes_gpt4_model():
    assert _normalize_chatgpt_model("gpt-4") == "gpt-4"


def test_normalizes_gpt4o_model():
    assert _normalize_chatgpt_model("gpt-4o") == "gpt-4o"


def test_normalizes_empty_model():
    assert _normalize_chatgpt_model("") == "unknown"


def test_normalizes_gpt54():
    assert _normalize_chatgpt_model("gpt-5.4") == "gpt-5.4"


def test_normalizes_gpt54_mini():
    assert _normalize_chatgpt_model("gpt-5.4-mini") == "gpt-5.4-mini"


def test_normalizes_gpt54_nano():
    assert _normalize_chatgpt_model("gpt-5.4-nano") == "gpt-5.4-nano"


def test_normalizes_gpt54_pro():
    assert _normalize_chatgpt_model("gpt-5.4-pro") == "gpt-5.4-pro"


def test_normalizes_gpt55():
    assert _normalize_chatgpt_model("gpt-5.5") == "gpt-5.5"


def test_normalizes_gpt55_pro():
    assert _normalize_chatgpt_model("gpt-5.5-pro") == "gpt-5.5-pro"


def test_preserves_versioned_codex():
    assert _normalize_chatgpt_model("gpt-5.3-codex") == "gpt-5.3-codex"


def test_preserves_codex_max_variant():
    assert _normalize_chatgpt_model("gpt-5.1-codex-max") == "gpt-5.1-codex-max"


def test_preserves_codenamed_variant():
    """Codenamed slugs (gpt-5.6-sol / -terra / -luna) must stay distinct —
    this is the bug this change fixes: they used to collapse to "gpt-5.6"."""
    assert _normalize_chatgpt_model("gpt-5.6-sol") == "gpt-5.6-sol"
    assert _normalize_chatgpt_model("gpt-5.6-terra") == "gpt-5.6-terra"
    assert _normalize_chatgpt_model("gpt-5.6-luna") == "gpt-5.6-luna"


def test_lowercases_and_strips():
    assert _normalize_chatgpt_model("  GPT-5.6-Sol  ") == "gpt-5.6-sol"


# ---------------------------------------------------------------------------
# Extraction from fixture
# ---------------------------------------------------------------------------


def test_extracts_response_messages_only():
    """Non-token_count records (turn_context, session_meta, user) are ignored;
    only the three token_count records with real info become events."""
    evts = parse_chatgpt_events(
        [FIXTURE],
        account_id="u@codex.test",
        since=datetime(2020, 1, 1, tzinfo=UTC),
    )
    assert len(evts) == 3
    assert all(e.provider_id == "chatgpt" for e in evts)


def test_preserves_full_model_slugs():
    """Full slugs are preserved end to end, not collapsed to a shared bucket
    ("gpt-5-codex" != "gpt-5.6-sol") or a bare version ("codex")."""
    evts = parse_chatgpt_events(
        [FIXTURE],
        account_id="u@codex.test",
        since=datetime(2020, 1, 1, tzinfo=UTC),
    )
    model_ids = {e.model_id for e in evts}
    # First event has no preceding turn_context → "unknown"
    # Second event follows turn_context {model: gpt-5-codex}
    # Third event follows turn_context {model: gpt-5.6-sol}
    assert model_ids == {"unknown", "gpt-5-codex", "gpt-5.6-sol"}


def test_effort_is_captured_and_not_sticky():
    """Effort is read from the turn_context in effect for each event, and a
    turn_context that omits effort resets it to None rather than inheriting
    the previous turn's value (unlike model, which IS sticky)."""
    evts = parse_chatgpt_events(
        [FIXTURE],
        account_id="u@codex.test",
        since=datetime(2020, 1, 1, tzinfo=UTC),
    )
    first = next(e for e in evts if e.model_id == "unknown")
    codex = next(e for e in evts if e.model_id == "gpt-5-codex")
    sol = next(e for e in evts if e.model_id == "gpt-5.6-sol")
    assert first.effort is None  # no turn_context seen yet
    assert codex.effort == "high"  # turn_context set effort: "high"
    assert sol.effort is None  # next turn_context omitted effort — not inherited


def test_session_id_from_filename():
    """session_id is the stem of the JSONL file."""
    evts = parse_chatgpt_events(
        [FIXTURE],
        account_id="u@codex.test",
        since=datetime(2020, 1, 1, tzinfo=UTC),
    )
    assert all(e.session_id == "chatgpt-sample" for e in evts)


def test_filters_by_since():
    """Events at or before since are excluded."""
    # Second event is at 14:10:00, third at 14:13:00; filter to exclude the
    # first (14:00:00).
    cutoff = datetime(2026, 5, 8, 14, 5, 0, tzinfo=UTC)
    evts = parse_chatgpt_events(
        [FIXTURE],
        account_id="u@codex.test",
        since=cutoff,
    )
    assert {e.model_id for e in evts} == {"gpt-5-codex", "gpt-5.6-sol"}


def test_captures_token_dimensions():
    """input, output, cache_read, reasoning are all populated correctly."""
    evts = parse_chatgpt_events(
        [FIXTURE],
        account_id="u@codex.test",
        since=datetime(2020, 1, 1, tzinfo=UTC),
    )
    # First event (gpt-5 / unknown model): raw input 1842 inclusive of 500 cached →
    # tokens_input = 1842 - 500 = 1342. Output 412, reasoning 0.
    first = next(e for e in evts if e.model_id == "unknown")
    assert first.tokens_input == 1342
    assert first.tokens_output == 412
    assert first.tokens_cache_read == 500
    assert first.tokens_reasoning == 0
    assert first.tokens_cache_create == 0  # fixture has no cache_write_input_tokens → defaults to 0

    # Second event (gpt-5-codex): 900 input, 300 output, 0 cached, 200 reasoning
    codex = next(e for e in evts if e.model_id == "gpt-5-codex")
    assert codex.tokens_input == 900
    assert codex.tokens_output == 300
    assert codex.tokens_cache_read == 0
    assert codex.tokens_reasoning == 200

    # Third event (gpt-5.6-sol): raw input 700 inclusive of 100 cached →
    # tokens_input = 700 - 100 = 600. Output 150, reasoning 50.
    sol = next(e for e in evts if e.model_id == "gpt-5.6-sol")
    assert sol.tokens_input == 600
    assert sol.tokens_output == 150
    assert sol.tokens_cache_read == 100
    assert sol.tokens_reasoning == 50


def test_captures_cwd_and_branch_from_session_meta():
    """cwd + git branch come from the session_meta header and apply to all events."""
    evts = parse_chatgpt_events(
        [FIXTURE],
        account_id="u@codex.test",
        since=datetime(2020, 1, 1, tzinfo=UTC),
    )
    assert len(evts) == 3
    assert all(e.cwd == "/home/user/codex-project" for e in evts)
    assert all(e.git_branch == "feat/widgets" for e in evts)


def test_missing_file_returns_empty():
    """Non-existent paths are silently skipped."""
    evts = parse_chatgpt_events(
        [Path("/does/not/exist.jsonl")],
        account_id="u@codex.test",
        since=datetime(2020, 1, 1, tzinfo=UTC),
    )
    assert evts == []


def test_captures_cache_write_tokens(tmp_path):
    """A record with a non-zero cache_write_input_tokens (gpt-5.6/gpt-6, per
    issue #369) surfaces tokens_cache_create, and tokens_input is reduced by
    both the cached and cache-write amounts — input_tokens from the Responses
    API is inclusive of both."""
    lines = [
        json.dumps(
            {
                "timestamp": "2026-09-20T10:00:00.000Z",
                "type": "session_meta",
                "payload": {"id": "sess", "cwd": "/tmp/proj"},
            }
        ),
        json.dumps(
            {
                "timestamp": "2026-09-20T10:00:01.000Z",
                "type": "turn_context",
                "payload": {"model": "gpt-6-sol"},
            }
        ),
        json.dumps(
            {
                "timestamp": "2026-09-20T10:00:02.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 1000,
                            "cached_input_tokens": 200,
                            "cache_write_input_tokens": 150,
                            "output_tokens": 300,
                            "reasoning_output_tokens": 10,
                            "total_tokens": 1300,
                        }
                    },
                },
            }
        ),
    ]
    fixture = tmp_path / "cache-write-sample.jsonl"
    fixture.write_text("\n".join(lines) + "\n", encoding="utf-8")

    evts = parse_chatgpt_events(
        [fixture],
        account_id="u@codex.test",
        since=datetime(2020, 1, 1, tzinfo=UTC),
    )
    assert len(evts) == 1
    evt = evts[0]
    assert evt.model_id == "gpt-6-sol"
    assert evt.tokens_cache_create == 150
    # tokens_input = 1000 (raw) - 200 (cached) - 150 (cache write) = 650
    assert evt.tokens_input == 650
    assert evt.tokens_cache_read == 200
    assert evt.tokens_output == 300
