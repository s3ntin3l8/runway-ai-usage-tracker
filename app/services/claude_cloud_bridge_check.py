"""Compare the cloud usage block of bridge sessions with Runway's local events.

A pure, offline check: no provider call, no write. ``rows`` are list rows reduced to
``{session_id, input, cache_read, cache_write}`` (any value may be ``None`` = unknown);
``local_totals`` are the sums of the same session's local ``usage_events``.

Output tokens are not compared (the cloud's assistant outputs are placeholders), and the
block's input is larger than the local input by the side model's input; see
``docs/collection_logic.md``. The report holds counts and numbers only, never an id.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from statistics import median
from typing import Any

from sqlalchemy import func
from sqlmodel import Session, col, select

from app.models.db_usage import UsageEvent
from app.services.claude_cloud_probe import session_key

FIELDS = ("input", "cache_read", "cache_write")
_ID_PREFIXES = ("", "session_", "cse_")
_CHUNK = 500


@dataclass(frozen=True)
class LocalTotals:
    """Sums of one session's local events."""

    input: int = 0
    cache_read: int = 0
    cache_write: int = 0


@dataclass
class FieldReport:
    """Per-field comparison; unknown (``None`` on either side) is never a mismatch."""

    exact_match: int = 0
    differing: int = 0
    unknown: int = 0
    largest_difference: int | None = None
    median_difference: float | None = None


@dataclass
class BridgeCheckReport:
    compared: int = 0
    cloud_only: int = 0
    without_block: int = 0
    fields: dict[str, FieldReport] = field(
        default_factory=lambda: {name: FieldReport() for name in FIELDS}
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "compared": self.compared,
            "cloud_only": self.cloud_only,
            "without_block": self.without_block,
            "fields": {
                name: {
                    "exact_match": rep.exact_match,
                    "differing": rep.differing,
                    "unknown": rep.unknown,
                    "largest_difference": rep.largest_difference,
                    "median_difference": rep.median_difference,
                }
                for name, rep in self.fields.items()
            },
        }


def compare_bridge_blocks(
    rows: Iterable[Mapping[str, Any]],
    local_totals: Mapping[str, LocalTotals],
) -> BridgeCheckReport:
    """Compare each row's block with the local totals of the same session.

    ``without_block``: every counter unknown. ``cloud_only``: a block but no local events.
    Otherwise the row is ``compared`` and each known pair is exact or differing.
    """
    report = BridgeCheckReport()
    diffs: dict[str, list[int]] = {name: [] for name in FIELDS}
    for row in rows:
        values = {name: row.get(name) for name in FIELDS}
        if all(v is None for v in values.values()):
            report.without_block += 1
            continue
        local = local_totals.get(session_key(str(row.get("session_id", ""))))
        if local is None:
            report.cloud_only += 1
            continue
        report.compared += 1
        for name in FIELDS:
            rep = report.fields[name]
            cloud = values[name]
            here = getattr(local, name)
            if cloud is None or here is None:
                rep.unknown += 1
                continue
            diff = abs(int(cloud) - int(here))
            diffs[name].append(diff)
            if diff == 0:
                rep.exact_match += 1
            else:
                rep.differing += 1
    for name, values_ in diffs.items():
        if values_:
            report.fields[name].largest_difference = max(values_)
            report.fields[name].median_difference = float(median(values_))
    return report


def load_local_totals(session: Session, keys: Iterable[str]) -> dict[str, LocalTotals]:
    """Sum the local Anthropic events of each session key, in one grouped query per chunk.

    Cloud-collector events (``sidecar_id = 'anthropic-cloud'``) are excluded: they are the
    cloud's own view, not an independent local one. A local session id may carry no prefix
    or ``session_``/``cse_``, so every form of a key is looked up and folded back by key.
    """
    wanted = {session_key(k) for k in keys}
    totals: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    ids = [p + k for k in sorted(wanted) for p in _ID_PREFIXES]
    for start in range(0, len(ids), _CHUNK):
        chunk = ids[start : start + _CHUNK]
        stmt = (
            select(
                UsageEvent.session_id,
                func.coalesce(func.sum(UsageEvent.tokens_input), 0),
                func.coalesce(func.sum(UsageEvent.tokens_cache_read), 0),
                func.coalesce(func.sum(UsageEvent.tokens_cache_create), 0),
            )
            .where(UsageEvent.provider_id == "anthropic")
            .where(UsageEvent.sidecar_id != "anthropic-cloud")
            .where(col(UsageEvent.session_id).in_(chunk))
            .group_by(UsageEvent.session_id)
        )
        for sid, tin, tread, twrite in session.exec(stmt):
            key = session_key(str(sid))
            if key not in wanted:
                continue
            acc = totals[key]
            acc[0] += int(tin)
            acc[1] += int(tread)
            acc[2] += int(twrite)
    return {k: LocalTotals(*v) for k, v in totals.items()}
