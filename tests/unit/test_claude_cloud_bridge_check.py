from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.models.db_usage import UsageEvent
from app.services.claude_cloud_bridge_check import (
    LocalTotals,
    compare_bridge_blocks,
    load_local_totals,
)
from scripts import check_bridge_sessions

ROOT = Path(__file__).resolve().parents[2]


def _row(sid: str, i: int | None = 10, r: int | None = 20, w: int | None = 30) -> dict:
    return {"session_id": sid, "input": i, "cache_read": r, "cache_write": w}


def test_exact_match_and_cse_row_matches_session_local_id() -> None:
    report = compare_bridge_blocks([_row("cse_abc")], {"abc": LocalTotals(10, 20, 30)})
    assert report.compared == 1
    for name in ("input", "cache_read", "cache_write"):
        assert report.fields[name].exact_match == 1
        assert report.fields[name].differing == 0
        assert report.fields[name].largest_difference == 0


def test_one_differing_field_reports_difference() -> None:
    report = compare_bridge_blocks([_row("cse_a", i=110)], {"a": LocalTotals(10, 20, 30)})
    assert report.fields["input"].differing == 1
    assert report.fields["input"].largest_difference == 100
    assert report.fields["input"].median_difference == 100.0
    assert report.fields["cache_read"].exact_match == 1


def test_none_is_unknown_not_zero_or_mismatch() -> None:
    report = compare_bridge_blocks([_row("cse_a", r=None)], {"a": LocalTotals(10, 0, 30)})
    rep = report.fields["cache_read"]
    assert (rep.unknown, rep.exact_match, rep.differing) == (1, 0, 0)
    assert rep.largest_difference is None and rep.median_difference is None


def test_cloud_only_and_without_block() -> None:
    rows = [_row("cse_none"), _row("cse_blockless", None, None, None)]
    report = compare_bridge_blocks(rows, {})
    assert (report.compared, report.cloud_only, report.without_block) == (0, 1, 1)


def test_median_over_rows() -> None:
    rows = [_row("cse_a", i=11), _row("cse_b", i=14), _row("cse_c", i=10)]
    local = {k: LocalTotals(10, 20, 30) for k in "abc"}
    rep = compare_bridge_blocks(rows, local).fields["input"]
    assert (rep.largest_difference, rep.median_difference) == (4, 1.0)


def _db() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _event(n: int, sid: str, sidecar: str, provider: str = "anthropic") -> UsageEvent:
    return UsageEvent(
        provider_id=provider,
        account_id="acc",
        sidecar_id=sidecar,
        event_id=f"e{n}",
        ts=datetime(2026, 11, 1, tzinfo=UTC),
        session_id=sid,
        tokens_input=1,
        tokens_cache_read=10,
        tokens_cache_create=100,
    )


def _seed(session: Session) -> None:
    session.add_all(
        [
            _event(1, "session_one", "laptop"),
            _event(2, "session_one", "laptop"),
            _event(3, "session_one", "anthropic-cloud"),  # excluded
            _event(4, "two", "laptop"),
            _event(5, "session_one", "laptop", provider="openai"),  # excluded
            _event(6, "session_other", "laptop"),  # not asked for
        ]
    )
    session.commit()


def test_load_local_totals_excludes_cloud_events() -> None:
    with _db() as session:
        _seed(session)
        totals = load_local_totals(session, ["cse_one", "two"])
    assert totals == {"one": LocalTotals(2, 20, 200), "two": LocalTotals(1, 10, 100)}


def test_script_main_prints_json_without_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = _db()
    _seed(session)
    monkeypatch.setattr(check_bridge_sessions, "engine", session.get_bind())
    rows = [
        {"session_id": "cse_one", "input": 5, "cache_read": 20, "cache_write": None},
        {"session_id": "cse_ghost", "input": 1, "cache_read": 1, "cache_write": 1},
        {"session_id": "cse_noblock", "input": None, "cache_read": None, "cache_write": None},
    ]
    path = tmp_path / "rows.json"
    path.write_text(json.dumps(rows))
    assert check_bridge_sessions.main(["--rows", str(path)]) == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert (data["compared"], data["cloud_only"], data["without_block"]) == (1, 1, 1)
    assert data["fields"]["input"]["largest_difference"] == 3
    assert data["fields"]["cache_read"]["exact_match"] == 1
    assert data["fields"]["cache_write"]["unknown"] == 1
    for sid in ("one", "ghost", "noblock"):
        assert sid not in out


def test_script_rejects_malformed_rows(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "rows.json"
    path.write_text('{"a": 1}')
    assert check_bridge_sessions.main(["--rows", str(path)]) == 2


def test_the_offline_check_has_no_provider_client() -> None:
    # The live reader is the sibling claude_cloud_bridge_rows; this module stays offline.
    src = (ROOT / "app/services/claude_cloud_bridge_check.py").read_text(encoding="utf-8")
    assert "httpx" not in src
    assert "claude_cloud_collector" not in src
