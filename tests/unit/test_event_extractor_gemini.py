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
    assert _normalize_gemini_model("gemini-2.5-flash") == "flash-2.5"


def test_normalizes_pro_model():
    assert _normalize_gemini_model("gemini-2.5-pro") == "pro-2.5"


def test_normalizes_flash_lite_model():
    assert _normalize_gemini_model("gemini-2.5-flash-lite") == "flash-lite-2.5"


def test_normalizes_gemini_3_pro_preview():
    """Gemini 3.x Pro has its own (higher) pricing — must not collapse to 2.5."""
    assert _normalize_gemini_model("gemini-3-pro-preview") == "pro-3.1-preview"
    assert _normalize_gemini_model("gemini-3.1-pro-preview") == "pro-3.1-preview"


def test_normalizes_gemini_3_flash_preview():
    """Gemini 3 Flash Preview gets its own bucket (Google published pricing 2026-05)."""
    assert _normalize_gemini_model("gemini-3-flash-preview") == "flash-3-preview"


def test_normalizes_gemini_3_1_flash_full():
    """Future non-preview 3.1 Flash lands in its own bucket."""
    assert _normalize_gemini_model("gemini-3.1-flash") == "flash-3.1"


def test_normalizes_gemini_3_1_flash_lite():
    assert _normalize_gemini_model("gemini-3.1-flash-lite") == "flash-lite-3.1"
    assert _normalize_gemini_model("gemini-3-flash-lite") == "flash-lite-3.1"


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
    """gemini-2.5-flash → flash-2.5, gemini-2.5-pro → pro-2.5."""
    evts = parse_gemini_events(
        [FIXTURE],
        account_id="u@gemini.test",
        since=datetime(2020, 1, 1, tzinfo=UTC),
    )
    model_ids = {e.model_id for e in evts}
    assert "flash-2.5" in model_ids
    assert "pro-2.5" in model_ids


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
    assert evts[0].model_id == "pro-2.5"


def test_captures_token_dimensions():
    """input is inclusive of cached — extractor subtracts to get fresh-only input."""
    evts = parse_gemini_events(
        [FIXTURE],
        account_id="u@gemini.test",
        since=datetime(2020, 1, 1, tzinfo=UTC),
    )
    # flash record has raw input=1500 inclusive of cached=500 → tokens_input = 1000
    flash = next(e for e in evts if e.model_id == "flash-2.5")
    assert flash.tokens_input == 1000
    assert flash.tokens_output == 200
    assert flash.tokens_cache_read == 500
    assert flash.tokens_reasoning == 0  # thoughts=0 in fixture

    # pro record has cached=0 → tokens_input unchanged
    pro = next(e for e in evts if e.model_id == "pro-2.5")
    assert pro.tokens_input == 800
    assert pro.tokens_output == 150
    assert pro.tokens_cache_read == 0
    assert pro.tokens_reasoning == 300  # thoughts=300 in fixture


def test_clamps_input_when_cached_exceeds_input(tmp_path: Path):
    """Defensive: if cached > input (shouldn't happen but guards against bad data),
    tokens_input clamps to 0 rather than going negative."""
    f = tmp_path / "weird.jsonl"
    f.write_text(
        '{"sessionId":"s","startTime":"2026-05-08T14:00:00.000Z","kind":"main"}\n'
        '{"id":"e1","timestamp":"2026-05-08T14:01:00.000Z","type":"gemini",'
        '"tokens":{"input":300,"output":50,"cached":500,"thoughts":0,"tool":0,"total":850},'
        '"model":"gemini-2.5-flash"}\n'
    )
    evts = parse_gemini_events(
        [f], account_id="u@gemini.test", since=datetime(2020, 1, 1, tzinfo=UTC)
    )
    assert len(evts) == 1
    assert evts[0].tokens_input == 0
    assert evts[0].tokens_cache_read == 500


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


def _write_gemini_session(base: Path, project: str, kind: str, project_root: str | None) -> Path:
    """Build a ~/.gemini/tmp-style <project>/chats/session-*.jsonl layout."""
    proj_dir = base / project
    chats = proj_dir / "chats"
    chats.mkdir(parents=True)
    if project_root is not None:
        (proj_dir / ".project_root").write_text(project_root, encoding="utf-8")
    fp = chats / "session-1.jsonl"
    fp.write_text(
        f'{{"sessionId":"s1","projectHash":"h","kind":"{kind}"}}\n'
        '{"id":"g1","timestamp":"2026-05-08T14:01:00.000Z","type":"gemini",'
        '"tokens":{"input":100,"output":20,"cached":0,"thoughts":0},"model":"gemini-2.5-flash"}\n',
        encoding="utf-8",
    )
    return fp


def test_cwd_from_project_root_file(tmp_path):
    """cwd is read from the sibling .project_root two levels up; main kind = no subagent."""
    fp = _write_gemini_session(tmp_path, "myproj", "main", "/home/user/repos/myproj")
    evts = parse_gemini_events([fp], account_id="u", since=datetime(2020, 1, 1, tzinfo=UTC))
    assert len(evts) == 1
    assert evts[0].cwd == "/home/user/repos/myproj"
    assert evts[0].subagent_type is None


def test_cwd_falls_back_to_dir_name_and_kind_maps_to_subagent(tmp_path):
    """Without .project_root, cwd falls back to the project dir name; non-main kind → subagent."""
    fp = _write_gemini_session(tmp_path, "fallbackproj", "plan", None)
    evts = parse_gemini_events([fp], account_id="u", since=datetime(2020, 1, 1, tzinfo=UTC))
    assert len(evts) == 1
    assert evts[0].cwd == "fallbackproj"
    assert evts[0].subagent_type == "plan"
