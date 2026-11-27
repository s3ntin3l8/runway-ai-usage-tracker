"""Per-chunk digests and a rolling audit of a transcript's consumed region.

The head-and-tail ``transcript_fingerprint`` proves a committed region on
every scan but misses a rewrite in its middle (#802, #1407). A transcript
cursor therefore also carries one digest per chunk of its committed region,
and a rolling audit re-reads a bounded number of chunks per scan across all
tracked transcripts. A middle rewrite is found within a few scans instead of
never, while the per-scan read stays bounded. The Cowork reader and the CLI
transcript reader share these helpers.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

CHUNK_BYTES = 256 * 1024
AUDIT_BYTES = 8 * 1024 * 1024
AUDIT_FIELDS = ("audit_next", "audit_done", "audited", "audit_mtime_ns")


def chunk_count(end: int) -> int:
    return -(-end // CHUNK_BYTES)


def chunk_span(index: int, end: int) -> tuple[int, int]:
    """Start and length of chunk ``index`` of the region ``[0, end)``."""
    lo = index * CHUNK_BYTES
    return lo, min(lo + CHUNK_BYTES, end) - lo


def chunk_digest(f: Any, index: int, end: int) -> str | None:
    """Digest chunk ``index`` of the region ``[0, end)``; None when the file is shorter."""
    lo, size = chunk_span(index, end)
    f.seek(lo)
    data = f.read(size)
    if len(data) != size:
        return None
    return hashlib.sha256(data).hexdigest()[:32]


def chunks_of(cursor: dict[str, Any], offset: int) -> list[str | None]:
    """The cursor's chunk digests; a state written before #802 has none yet (all None)."""
    chunks = cursor.get("chunks")
    if (
        isinstance(chunks, list)
        and len(chunks) == chunk_count(offset)
        and all(c is None or isinstance(c, str) for c in chunks)
    ):
        return list(chunks)
    return [None] * chunk_count(offset)


def extend_chunks(path: Path, prior: list[str | None], start: int, end: int) -> list[str | None]:
    """Keep the chunks wholly below ``start`` and digest the rest of ``[0, end)``."""
    base = start // CHUNK_BYTES
    chunks = prior[:base] + [None] * max(0, base - len(prior))
    with path.open("rb") as f:
        chunks.extend(chunk_digest(f, i, end) for i in range(base, chunk_count(end)))
    return chunks


def audit(
    state: dict[str, Any],
    tracked: dict[str, os.stat_result],
    budget: int,
    same_file: Callable[[dict[str, Any], os.stat_result], bool],
) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    """Re-read up to ``budget`` bytes of committed chunks; drop a cursor whose chunk changed.

    Every eligible transcript gets an equal slice of the budget, at least one
    chunk, and no chunk is read past the budget, so one large transcript
    cannot starve the others. With more eligible transcripts than chunks in
    the budget, only the first ones in sort order are served in a scan; a
    transcript keeps its place until its pass completes and then moves back. A pass over a
    transcript's chunks may span many scans: ``audit_done`` counts the chunks
    checked in the current pass, and the pass completes, setting ``audited``
    and ``audit_mtime_ns``, once it reaches the chunk count. Transcripts
    written since their last completed pass go first, then the one whose pass
    completed longest ago. A missing digest (a state from before #802) is
    filled in from the current bytes, which starts its coverage from now.
    ``same_file`` says whether a cursor still describes the file behind
    ``stat`` (same device and inode). Returns the updated cursors and the
    offsets of the dropped ones, both keyed by path.
    """
    updated: dict[str, dict[str, Any]] = {}
    dropped: dict[str, int] = {}
    order = []
    for key, stat in tracked.items():
        cursor = state.get(key)
        if (
            isinstance(cursor, dict)
            and isinstance(cursor.get("offset"), int)
            and 0 < cursor["offset"] <= stat.st_size
            and same_file(cursor, stat)
        ):
            audited = cursor.get("audited")
            order.append(
                (
                    cursor.get("audit_mtime_ns") == stat.st_mtime_ns,
                    audited if isinstance(audited, (int, float)) else 0,
                    key,
                )
            )
    if not order:
        return updated, dropped
    share = max(CHUNK_BYTES, budget // len(order))
    for _, _, key in sorted(order):
        if budget <= 0:
            break
        allowance = min(share, budget)
        cursor = state[key]
        offset = cursor["offset"]
        chunks = chunks_of(cursor, offset)
        count = len(chunks)
        index = cursor.get("audit_next")
        index = index % count if isinstance(index, int) and index >= 0 else 0
        done = cursor.get("audit_done")
        done = min(done, count) if isinstance(done, int) and done >= 0 else 0
        complete = False
        changed = False
        try:
            with Path(key).open("rb") as f:
                while not complete:
                    _, size = chunk_span(index, offset)
                    if size > allowance:
                        break
                    digest = chunk_digest(f, index, offset)
                    allowance -= size
                    budget -= size
                    if digest is None or chunks[index] not in (None, digest):
                        changed = True
                        break
                    chunks[index] = digest
                    index = (index + 1) % count
                    done += 1
                    complete = done >= count
        except OSError:
            continue
        if changed:
            logging.warning("Transcript changed inside consumed history; replaying it")
            dropped[key] = offset
            del state[key]
            continue
        cursor = dict(cursor, chunks=chunks, audit_next=index, audit_done=done)
        if complete:
            cursor.update(
                audit_done=0, audited=time.time(), audit_mtime_ns=tracked[key].st_mtime_ns
            )
        state[key] = updated[key] = cursor
    return updated, dropped
