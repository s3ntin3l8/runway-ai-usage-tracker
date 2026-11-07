"""Read the live session list for the bridge-session comparison.

The producer of ``claude_cloud_bridge_check.compare_bridge_blocks``' rows. It calls the provider (a
read-only ``GET /v1/code/sessions``), so it lives apart from the offline check, which must stay free
of any provider client. Paging, headers and login handling are the collector's.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, NamedTuple, TypedDict

import httpx

from app.services import claude_cloud_collector as collector
from app.services import claude_cloud_probe as probe
from app.services import claude_cloud_support as support
from app.services.probe_mode import probing

logger = logging.getLogger(__name__)

BRIDGE_KIND = "bridge"
# Cloud usage-block key of each compared counter.
_BLOCK_KEYS = {
    "input": "input_tokens",
    "cache_read": "cache_read_tokens",
    "cache_write": "cache_write_tokens",
}


class BridgeRow(TypedDict):
    """One bridge list row reduced to its usage block; ``None`` = the API did not report it."""

    session_id: str
    input: int | None
    cache_read: int | None
    cache_write: int | None


class BridgeSource(NamedTuple):
    """The exact cached Claude Code login (account slot and source id) the list is read with."""

    account: str
    source_id: str


def _count(value: object) -> int | None:
    # Same rule as the collector's block reader: a non-negative int, else unknown.
    return value if type(value) is int and 0 <= value < 2**53 else None


def _bridge_row(row: dict[str, Any]) -> BridgeRow | None:
    metadata = row.get("external_metadata")
    usage = metadata.get("usage") if isinstance(metadata, dict) else None
    if not isinstance(usage, dict):
        return None
    counts = {name: _count(usage.get(key)) for name, key in _BLOCK_KEYS.items()}
    if all(value is None for value in counts.values()):
        return None
    return BridgeRow(
        session_id=row["id"],
        input=counts["input"],
        cache_read=counts["cache_read"],
        cache_write=counts["cache_write"],
    )


async def read_bridge_rows(client: httpx.AsyncClient, source: BridgeSource) -> list[BridgeRow]:
    """Page the live session list and return the bridge rows that carry a usage block.

    Uses the collector's list bounds, headers and read-only login handling (no refresh, account
    and organization checks, bounded responses, the shared endpoint gate). Raises
    ``claude_cloud_support.ProbeError`` with a fixed outcome name when the read cannot be trusted.
    A list cut short at the page cap returns the rows read so far, as the collector does.
    """
    if support.gate.locked():
        raise support.ProbeError("busy")
    attempt = collector.new_probe(source.account, source.source_id, client)
    try:
        async with asyncio.timeout(collector.CYCLE_TIMEOUT):
            with probing():
                attempt.tokens, attempt.expected_org, attempt.authority = await probe.load_source(
                    attempt.account, source.source_id
                )
                async with probe.token_cache.using_source(
                    "anthropic", attempt.account, source.source_id
                ):
                    headers = await attempt.identity()
                    read = await support.read_session_list(
                        attempt,
                        headers,
                        parse_page=probe.page_with_terminal_omission,
                        limit=collector.LIST_LIMIT,
                        max_pages=collector.MAX_LIST_PAGES,
                        pause=collector.PAGE_PAUSE,
                        keep=lambda row: row.get("environment_kind") == BRIDGE_KIND,
                    )
                    attempt.report["stage"] = "recheck"
                    await attempt.check_source()
    except TimeoutError:
        raise support.ProbeError("timeout") from None
    except httpx.HTTPError:
        raise support.ProbeError("unavailable") from None
    if "max_pages" in read.notes or "cursor_repeat" in read.notes:
        logger.warning("Bridge session list cut short (%s)", ",".join(read.notes))
    return [bridge for row in read.rows if (bridge := _bridge_row(row)) is not None]


async def list_bridge_sources() -> list[BridgeSource]:
    """The cached Claude Code logins in the collector's order: per account, sorted by source id."""
    sources: set[BridgeSource] = set()
    for candidate in await probe.token_cache.get_account_source_candidates("anthropic"):
        slot, source_id = candidate.get("account_slot"), candidate.get("source_id")
        if isinstance(slot, str) and isinstance(source_id, str):
            sources.add(BridgeSource(slot, source_id))
    return sorted(sources)


async def read_all_bridge_rows(client: httpx.AsyncClient) -> list[BridgeRow]:
    """``read_bridge_rows`` once per account, through its first login that works.

    Two logins of one account see the same sessions, so a failing login falls through to the
    next, as in the collector. Raises the last ``ProbeError`` when an account has no login that
    works, and ``source_unavailable`` when no login is cached at all.
    """
    sources = await list_bridge_sources()
    if not sources:
        raise support.ProbeError("source_unavailable")
    rows: list[BridgeRow] = []
    failure: support.ProbeError | None = None
    done: set[str] = set()
    for source in sources:
        if source.account in done:
            continue
        try:
            rows.extend(await read_bridge_rows(client, source))
        except support.ProbeError as exc:
            failure = exc
            continue
        done.add(source.account)
    if failure is not None and len(done) < len({s.account for s in sources}):
        raise failure
    return rows
