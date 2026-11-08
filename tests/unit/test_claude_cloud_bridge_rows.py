"""The live producer of the bridge comparison and the script's ``--live`` flag (mock transport only)."""

# ruff: noqa: F811  (fixtures are imported, then requested by name)

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, create_engine

from app.services import claude_cloud_collector as collector
from app.services import claude_cloud_support as support
from app.services.claude_cloud_bridge_rows import BridgeSource, read_bridge_rows
from scripts import check_bridge_sessions
from tests.unit.test_claude_cloud_collector import (  # noqa: F401  (fixtures)
    ACCOUNT,
    SOURCE,
    Server,
    bridge_row,
    cloud_row,
    environment,
    no_page_pause,
)

SOURCE_REF = BridgeSource(ACCOUNT, SOURCE)


def with_block(sid: str, **usage: int) -> dict:
    row = bridge_row(sid)
    row["external_metadata"] = {"usage": usage}
    return row


async def read(server: Server) -> list:
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        return await read_bridge_rows(client, SOURCE_REF)


@pytest.mark.asyncio
async def test_only_bridge_rows_with_a_block_are_returned_and_gaps_stay_unknown(environment):
    server = Server(
        [
            with_block(
                "cse_one",
                input_tokens=7,
                cache_read_tokens=9,
                cache_write_tokens=11,
                output_tokens=3,
            ),
            with_block("cse_partial", input_tokens=4),
            bridge_row("cse_blockless"),
            with_block("cse_junk", input_tokens=-1, cache_read_tokens=True),
            cloud_row("cse_cloudrunner", block={"input_tokens": 1}),
        ],
        {},
    )
    assert await read(server) == [
        {"session_id": "cse_one", "input": 7, "cache_read": 9, "cache_write": 11},
        {"session_id": "cse_partial", "input": 4, "cache_read": None, "cache_write": None},
    ]


@pytest.mark.asyncio
async def test_pagination_is_followed_to_the_last_page(environment, monkeypatch):
    monkeypatch.setattr(collector, "LIST_LIMIT", 2)
    rows = [with_block(f"cse_s{i}", input_tokens=i) for i in range(5)]
    server = Server(rows, {})
    result = await read(server)
    assert [r["session_id"] for r in result] == [f"cse_s{i}" for i in range(5)]
    assert len(server.calls) == 3


@pytest.mark.asyncio
async def test_listing_stops_at_the_collectors_page_cap(environment, monkeypatch):
    monkeypatch.setattr(collector, "LIST_LIMIT", 1)
    monkeypatch.setattr(collector, "MAX_LIST_PAGES", 2)
    server = Server([with_block(f"cse_s{i}", input_tokens=i) for i in range(6)], {})
    result = await read(server)
    assert len(server.calls) == 2
    assert [r["session_id"] for r in result] == ["cse_s0", "cse_s1"]


@pytest.mark.asyncio
async def test_a_busy_endpoint_makes_no_request(environment):
    server = Server([], {})
    async with support.gate:
        with pytest.raises(support.ProbeError, match="busy"):
            await read(server)
    assert server.calls == []


@pytest.mark.asyncio
async def test_a_duplicate_row_id_breaks_the_contract(environment):
    server = Server([with_block("cse_a", input_tokens=1), with_block("cse_a", input_tokens=1)], {})
    with pytest.raises(support.ProbeError, match="unsupported_contract"):
        await read(server)


# -- the script ---------------------------------------------------------------------------------


@pytest.fixture
def script_db(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(check_bridge_sessions, "engine", engine)
    return engine


def patch_client(monkeypatch, server: Server) -> None:
    real = httpx.AsyncClient
    monkeypatch.setattr(
        check_bridge_sessions.httpx,
        "AsyncClient",
        lambda *a, **k: real(transport=httpx.MockTransport(server)),
    )


def test_live_without_the_consent_flag_refuses_and_makes_no_request(monkeypatch, capsys, script_db):
    server = Server([with_block("cse_secretid", input_tokens=1)], {})
    patch_client(monkeypatch, server)
    assert check_bridge_sessions.main(["--live"]) == 2
    assert server.calls == []
    assert "--yes-call-provider" in capsys.readouterr().err


def test_the_consent_flag_alone_is_refused(capsys, script_db, tmp_path):
    path = tmp_path / "rows.json"
    path.write_text("[]")
    assert check_bridge_sessions.main(["--rows", str(path), "--yes-call-provider"]) == 2


@pytest.mark.asyncio
async def test_live_report_is_counts_only(environment, monkeypatch, capsys, script_db):
    server = Server(
        [
            with_block("cse_secretid", input_tokens=5, cache_read_tokens=1, cache_write_tokens=2),
            bridge_row("cse_blockless"),
        ],
        {},
    )
    patch_client(monkeypatch, server)
    # The in-process fixture already holds the login; keep the durable path off.
    monkeypatch.setattr(check_bridge_sessions.source_controls, "initialize", lambda: None)
    # The script owns its event loop, so it runs off the test's.
    code = await asyncio.to_thread(check_bridge_sessions.main, ["--live", "--yes-call-provider"])
    assert code == 0
    assert [c[0] for c in server.calls] == ["/v1/code/sessions"]
    out = capsys.readouterr().out
    report = json.loads(out)
    assert (report["compared"], report["cloud_only"], report["without_block"]) == (0, 1, 0)
    assert "secretid" not in out and "blockless" not in out


def test_live_failure_exits_nonzero_with_a_fixed_outcome(monkeypatch, capsys, script_db):
    async def boom():
        raise support.ProbeError("rejected")

    monkeypatch.setattr(check_bridge_sessions, "_live_rows", boom)
    assert check_bridge_sessions.main(["--live", "--yes-call-provider"]) == 3
    assert "rejected" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_live_rows_enable_the_durable_snapshot_before_reading(monkeypatch):
    # A fresh process only hydrates its login cache once source controls are enabled.
    order: list[str] = []

    async def fake_read(client):
        order.append("read")
        return []

    monkeypatch.setattr(
        check_bridge_sessions.source_controls, "initialize", lambda: order.append("init")
    )
    monkeypatch.setattr(check_bridge_sessions, "read_all_bridge_rows", fake_read)
    assert await check_bridge_sessions._live_rows() == []
    assert order == ["init", "read"]


def test_live_on_a_database_without_the_sidecar_tables_exits_with_source_unavailable(
    monkeypatch, capsys
):
    from sqlalchemy.exc import OperationalError

    def broken() -> None:
        raise OperationalError("select", {}, Exception("no such table"))

    monkeypatch.setattr(check_bridge_sessions.source_controls, "initialize", broken)
    assert check_bridge_sessions.main(["--live", "--yes-call-provider"]) == 3
    assert "source_unavailable" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_a_network_timeout_is_reported_as_timeout(environment):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(support.ProbeError, match="timeout"):
            await read_bridge_rows(client, SOURCE_REF)
