"""Unit tests for the ChatGPT/Codex event extractor."""

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


def test_normalizes_codex_model():
    assert _normalize_chatgpt_model("gpt-5-codex") == "codex"


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


def test_normalizes_versioned_codex():
    assert _normalize_chatgpt_model("gpt-5.3-codex") == "codex"


# ---------------------------------------------------------------------------
# Extraction from fixture
# ---------------------------------------------------------------------------


def test_extracts_response_messages_only():
    """Non-token_count and null-info records are ignored."""
    evts = parse_chatgpt_events(
        [FIXTURE],
        account_id="u@codex.test",
        since=datetime(2020, 1, 1, tzinfo=UTC),
    )
    # Fixture has 2 token_count records with real info, 1 null-info record, 1 user record
    assert len(evts) == 2
    assert all(e.provider_id == "chatgpt" for e in evts)


def test_normalizes_model_ids():
    """The second token_count event follows a turn_context with gpt-5-codex."""
    evts = parse_chatgpt_events(
        [FIXTURE],
        account_id="u@codex.test",
        since=datetime(2020, 1, 1, tzinfo=UTC),
    )
    model_ids = {e.model_id for e in evts}
    # First event has no preceding turn_context → "unknown"
    # Second event follows turn_context with gpt-5-codex → "codex"
    assert "unknown" in model_ids
    assert "codex" in model_ids


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
    # The second event is at 14:10:00; filter to exclude first (14:00:00)
    cutoff = datetime(2026, 5, 8, 14, 5, 0, tzinfo=UTC)
    evts = parse_chatgpt_events(
        [FIXTURE],
        account_id="u@codex.test",
        since=cutoff,
    )
    assert len(evts) == 1
    assert evts[0].model_id == "codex"


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
    assert first.tokens_cache_create == 0  # OpenAI doesn't bill cache creation

    # Second event (codex): 900 input, 300 output, 0 cached, 200 reasoning
    codex = next(e for e in evts if e.model_id == "codex")
    assert codex.tokens_input == 900
    assert codex.tokens_output == 300
    assert codex.tokens_cache_read == 0
    assert codex.tokens_reasoning == 200


def test_captures_cwd_and_branch_from_session_meta():
    """cwd + git branch come from the session_meta header and apply to all events."""
    evts = parse_chatgpt_events(
        [FIXTURE],
        account_id="u@codex.test",
        since=datetime(2020, 1, 1, tzinfo=UTC),
    )
    assert len(evts) == 2
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
