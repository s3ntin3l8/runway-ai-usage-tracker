"""Unit tests for the Gemini event extractor."""

from datetime import UTC, datetime
from pathlib import Path

from scripts.sidecar_pkg.event_extractors.gemini import (
    _normalize_gemini_model,
    parse_gemini_events,
)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "gemini-sample.jsonl"


# ---------------------------------------------------------------------------
# Model normalisation
# ---------------------------------------------------------------------------


def test_normalizes_flash_model():
    assert _normalize_gemini_model("gemini-2.5-flash") == "flash"


def test_normalizes_pro_model():
    assert _normalize_gemini_model("gemini-2.5-pro") == "pro"


def test_normalizes_flash_lite_model():
    assert _normalize_gemini_model("gemini-2.5-flash-lite") == "flash-lite"


def test_normalizes_flash_preview():
    assert _normalize_gemini_model("gemini-3-flash-preview") == "flash"


def test_normalizes_empty_model():
    assert _normalize_gemini_model("") == "unknown"


# ---------------------------------------------------------------------------
# Extraction from fixture
# ---------------------------------------------------------------------------


def test_extracts_gemini_type_records_only():
    """Non-gemini type records (user, metadata, $set) are ignored."""
    evts = parse_gemini_events(
        [FIXTURE],
        account_id="u@gemini.test",
        since=datetime(2020, 1, 1, tzinfo=UTC),
    )
    # Fixture has 2 gemini records with tokens and 2 non-gemini records
    assert len(evts) == 2
    assert all(e.provider_id == "gemini" for e in evts)


def test_normalizes_model_ids():
    """gemini-2.5-flash → flash, gemini-2.5-pro → pro."""
    evts = parse_gemini_events(
        [FIXTURE],
        account_id="u@gemini.test",
        since=datetime(2020, 1, 1, tzinfo=UTC),
    )
    model_ids = {e.model_id for e in evts}
    assert "flash" in model_ids
    assert "pro" in model_ids


def test_session_id_from_filename():
    """session_id is the stem of the JSONL file."""
    evts = parse_gemini_events(
        [FIXTURE],
        account_id="u@gemini.test",
        since=datetime(2020, 1, 1, tzinfo=UTC),
    )
    assert all(e.session_id == "gemini-sample" for e in evts)


def test_filters_by_since():
    """Events at or before since are excluded."""
    cutoff = datetime(2026, 5, 8, 14, 3, 0, tzinfo=UTC)
    evts = parse_gemini_events(
        [FIXTURE],
        account_id="u@gemini.test",
        since=cutoff,
    )
    # Only the pro record (14:05:00) is after the cutoff
    assert len(evts) == 1
    assert evts[0].model_id == "pro"


def test_captures_token_dimensions():
    """input, output, cache_read (cached), reasoning (thoughts) are populated."""
    evts = parse_gemini_events(
        [FIXTURE],
        account_id="u@gemini.test",
        since=datetime(2020, 1, 1, tzinfo=UTC),
    )
    flash = next(e for e in evts if e.model_id == "flash")
    assert flash.tokens_input == 1000
    assert flash.tokens_output == 200
    assert flash.tokens_cache_read == 500
    assert flash.tokens_reasoning == 0  # thoughts=0 in fixture

    pro = next(e for e in evts if e.model_id == "pro")
    assert pro.tokens_input == 800
    assert pro.tokens_output == 150
    assert pro.tokens_cache_read == 0
    assert pro.tokens_reasoning == 300  # thoughts=300 in fixture


def test_event_id_from_record_id():
    """Gemini records have an id field that is used as event_id."""
    evts = parse_gemini_events(
        [FIXTURE],
        account_id="u@gemini.test",
        since=datetime(2020, 1, 1, tzinfo=UTC),
    )
    event_ids = {e.event_id for e in evts}
    assert "evt-001" in event_ids
    assert "evt-002" in event_ids


def test_missing_file_returns_empty():
    """Non-existent paths are silently skipped."""
    evts = parse_gemini_events(
        [Path("/does/not/exist.jsonl")],
        account_id="u@gemini.test",
        since=datetime(2020, 1, 1, tzinfo=UTC),
    )
    assert evts == []
