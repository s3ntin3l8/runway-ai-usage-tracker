"""Manifest-scoped macOS Cowork tokens; never read audit logs or quota history."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.sidecar_pkg.event_extractors.anthropic import parse_anthropic_events
from scripts.sidecar_pkg.event_watermark import transcript_fingerprint as _fingerprint
from scripts.sidecar_pkg.identity import canonical_account_id


def _load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
        return (
            data["files"]
            if data.get("version") == 1 and isinstance(data.get("files"), dict)
            else {}
        )
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def _write(path: Path, files: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".cowork-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"version": 1, "files": files}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


# The head-and-tail fingerprint (shared with the CLI reader) proves the
# committed region on every scan but misses a rewrite in its middle (#802).
# Each cursor therefore also carries one digest per chunk of the committed
# region, and a rolling audit re-reads a bounded number of chunks per scan
# across all tracked transcripts. A middle rewrite is found within a few
# scans instead of never, while the per-scan read stays bounded.
_CHUNK_BYTES = 256 * 1024
_AUDIT_BYTES = 8 * 1024 * 1024
_AUDIT_FIELDS = ("audit_next", "audit_done", "audited", "audit_mtime_ns")


def _chunk_count(end: int) -> int:
    return -(-end // _CHUNK_BYTES)


def _chunk_span(index: int, end: int) -> tuple[int, int]:
    """Start and length of chunk ``index`` of the region ``[0, end)``."""
    lo = index * _CHUNK_BYTES
    return lo, min(lo + _CHUNK_BYTES, end) - lo


def _chunk_digest(f: Any, index: int, end: int) -> str | None:
    """Digest chunk ``index`` of the region ``[0, end)``; None when the file is shorter."""
    lo, size = _chunk_span(index, end)
    f.seek(lo)
    data = f.read(size)
    if len(data) != size:
        return None
    return hashlib.sha256(data).hexdigest()[:32]


def _chunks_of(cursor: dict[str, Any], offset: int) -> list[str | None]:
    """The cursor's chunk digests; a state written before #802 has none yet (all None)."""
    chunks = cursor.get("chunks")
    if (
        isinstance(chunks, list)
        and len(chunks) == _chunk_count(offset)
        and all(c is None or isinstance(c, str) for c in chunks)
    ):
        return list(chunks)
    return [None] * _chunk_count(offset)


def _extend_chunks(path: Path, prior: list[str | None], start: int, end: int) -> list[str | None]:
    """Keep the chunks wholly below ``start`` and digest the rest of ``[0, end)``."""
    base = start // _CHUNK_BYTES
    chunks = prior[:base] + [None] * max(0, base - len(prior))
    with path.open("rb") as f:
        chunks.extend(_chunk_digest(f, i, end) for i in range(base, _chunk_count(end)))
    return chunks


def _audit(
    state: dict[str, Any], tracked: dict[str, os.stat_result], budget: int
) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    """Re-read up to ``budget`` bytes of committed chunks; drop a cursor whose chunk changed.

    Every eligible transcript gets an equal slice of the budget (at least one
    chunk), so one large transcript cannot starve the others. A pass over a
    transcript's chunks may span many scans: ``audit_done`` counts the chunks
    checked in the current pass, and the pass completes, setting ``audited``
    and ``audit_mtime_ns``, once it reaches the chunk count. Transcripts
    written since their last completed pass go first, then the one whose pass
    completed longest ago. A missing digest (a state from before #802) is
    filled in from the current bytes, which starts its coverage from now.
    Returns the updated cursors and the offsets of the dropped ones, both
    keyed by path.
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
            and cursor.get("device") == stat.st_dev
            and cursor.get("inode") == stat.st_ino
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
    share = max(_CHUNK_BYTES, budget // len(order))
    for _, _, key in sorted(order):
        if budget <= 0:
            break
        allowance = min(share, budget)
        cursor = state[key]
        offset = cursor["offset"]
        chunks = _chunks_of(cursor, offset)
        count = len(chunks)
        index = cursor.get("audit_next")
        index = index % count if isinstance(index, int) and index >= 0 else 0
        done = cursor.get("audit_done")
        done = min(done, count) if isinstance(done, int) and done >= 0 else 0
        complete = False
        changed = False
        try:
            with Path(key).open("rb") as f:
                while allowance > 0 and not complete:
                    _, size = _chunk_span(index, offset)
                    digest = _chunk_digest(f, index, offset)
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
            logging.warning("Cowork transcript changed inside consumed history; replaying it")
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


def _save_audit(path: Path, updated: dict[str, dict[str, Any]], dropped: dict[str, int]) -> None:
    """Merge audit results into the state on disk, only where its cursor did not move."""
    if not updated and not dropped:
        return
    data = _load(path)
    for key, cursor in updated.items():
        disk = data.get(key)
        if isinstance(disk, dict) and disk.get("offset") == cursor["offset"]:
            data[key] = cursor
    for key, offset in dropped.items():
        disk = data.get(key)
        if isinstance(disk, dict) and disk.get("offset") == offset:
            del data[key]
    _write(path, data)


def _within(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


@dataclass
class CoworkBatch:
    """A scan and its uncommitted checkpoints, owned only at transport boundaries."""

    events: list[dict[str, Any]]
    blocked: list[dict[str, str]]
    state_path: Path
    checkpoints: dict[str, tuple[dict[str, Any], dict[tuple[str, str], int]]] = field(
        default_factory=dict
    )
    owned: dict[tuple[str, str], int] = field(default_factory=dict)

    def mark_owned(self, events: list[dict[str, Any]]) -> None:
        for event in events:
            if event.get("provider_id") == "anthropic":
                key = (event["event_id"], event["account_id"])
                self.owned[key] = max(self.owned.get(key, -1), event["tokens_output"])
        ready = {
            path: cursor
            for path, (cursor, required) in self.checkpoints.items()
            if all(self.owned.get(key, -1) >= output for key, output in required.items())
        }
        if not ready:
            return
        data = _load(self.state_path)
        data.update(ready)
        _write(self.state_path, data)
        for k in ready:
            del self.checkpoints[k]


def collect_cowork(
    base: Path,
    state_path: Path,
    tag_hints: dict[str, str],
    *,
    max_bytes: int = 4 * 1024 * 1024,
    max_events: int = 500,
    audit_bytes: int = _AUDIT_BYTES,
    existing_events: list[dict[str, Any]] | None = None,
) -> CoworkBatch:
    """Backfill complete lines independently of CLI timestamps, oldest files first."""
    batch = CoworkBatch([], [], state_path)
    state = _load(state_path)
    seen_paths: set[Path] = set()
    seen = {
        event["event_id"]: event
        for event in existing_events or ()
        if event["provider_id"] == "anthropic"
    }
    manifests: list[tuple[Path, dict[str, Any]]] = []
    identities: dict[Path, set[str]] = {}
    for path in sorted(base.glob("*/*/local_*.json")):
        if not _within(path, base):
            continue
        try:
            metadata = json.loads(path.read_text())
            if not isinstance(metadata, dict) or metadata.get("sessionId") != path.stem:
                continue
            cli_id = metadata.get("cliSessionId")
            if not isinstance(cli_id, str) or not cli_id:
                continue
            manifests.append((path, metadata))
            email = metadata.get("emailAddress")
            if isinstance(email, str) and email.strip():
                identities.setdefault(path.parent, set()).add(canonical_account_id(email))
        except (OSError, ValueError):
            logging.warning("Cowork session metadata unavailable")
    candidates: list[tuple[float, Path, str, str, str]] = []
    blocked_origins: set[str] = set()
    for manifest, metadata in manifests:
        root = manifest.parent.resolve()
        origin = f"path:{root}"
        emails = identities.get(manifest.parent, set())
        if len(emails) > 1:
            blocked_origins.add(origin)
            logging.warning("Cowork identity conflict; session logs quarantined")
            continue
        email = metadata.get("emailAddress")
        local = email if isinstance(email, str) and email.strip() else None
        account = canonical_account_id(local or tag_hints.get(origin))
        source = "local" if local else "tag" if tag_hints.get(origin) else "default"
        logs = manifest.with_suffix("") / ".claude" / "projects"
        if not _within(logs, root):
            continue
        for path in logs.glob("**/*.jsonl"):
            resolved = path.resolve()
            if resolved in seen_paths or not _within(path, logs) or path.name == "audit.jsonl":
                continue
            seen_paths.add(resolved)
            if source == "default":
                blocked_origins.add(origin)
            try:
                candidates.append((path.stat().st_mtime, resolved, account, source, origin))
            except OSError:
                logging.warning("Cowork transcript unavailable")
    tracked: dict[str, os.stat_result] = {}
    for _, path, _, _, _ in candidates:
        try:
            tracked[str(path)] = path.stat()
        except OSError:
            continue
    try:
        _save_audit(state_path, *_audit(state, tracked, audit_bytes))
    except OSError:
        logging.warning("Cowork transcript audit unavailable")
    remaining = max_bytes
    for _, path, account, source, origin in sorted(candidates):
        if remaining <= 0 or len(batch.events) >= max_events:
            break
        try:
            stat = path.stat()
            old = state.get(str(path), {})
            if not isinstance(old, dict):
                old = {}
            start = old.get("offset", 0)
            # The committed prefix is proven unchanged by its content
            # fingerprint, never by its timestamp: a transcript rewritten in
            # place inside one filesystem timestamp tick keeps its mtime_ns,
            # and a timestamp shortcut would skip that rewrite silently.
            valid = (
                isinstance(start, int)
                and 0 <= start <= stat.st_size
                and old.get("device") == stat.st_dev
                and old.get("inode") == stat.st_ino
                and old.get("fingerprint") == _fingerprint(path, start)
            )
            if not valid:
                start = 0
            elif old.get("account") not in (None, "default", account):
                blocked_origins.add(origin)
                logging.warning("Cowork transcript identity changed; quarantined")
                continue
            end = start
            assistant_records = 0
            with path.open("rb") as f:
                f.seek(start)
                while remaining > 0 and assistant_records < max_events - len(batch.events):
                    # Permit one complete large record, with a fixed 16 MiB cap,
                    # so a record larger than the normal budget cannot starve history.
                    line = f.readline(max(remaining, 16 * 1024 * 1024) + 1)
                    if not line or not line.endswith(b"\n"):
                        break
                    if len(line) > 16 * 1024 * 1024:
                        logging.warning("Cowork transcript record exceeds 16 MiB; retry pending")
                        break
                    if len(line) > remaining and end != start:
                        break
                    remaining -= len(line)
                    end = f.tell()
                    try:
                        record = json.loads(line)
                        if isinstance(record, dict) and record.get("type") == "assistant":
                            assistant_records += 1
                    except (ValueError, UnicodeError):
                        logging.warning("Cowork malformed transcript record skipped")
            if end == start:
                continue
            parsed = parse_anthropic_events(
                [path], account, datetime.min.replace(tzinfo=UTC), ranges={path: (start, end)}
            )
            required: dict[tuple[str, str], int] = {}
            for event in parsed:
                payload = seen.get(event.event_id)
                # Keep the first producer's account for overlapping files. An
                # acknowledged smaller payload on that account does not cover
                # a larger update from this range.
                if payload is None or (
                    payload["account_id"] == account
                    and event.tokens_output > payload["tokens_output"]
                ):
                    payload = event.model_dump(mode="json")
                    payload["account_source"] = source
                    payload["credential_origin"] = origin
                    seen[event.event_id] = payload
                    batch.events.append(payload)
                required[(event.event_id, payload["account_id"])] = payload["tokens_output"]
            if source == "default" and required:
                blocked_origins.add(origin)
            batch.checkpoints[str(path)] = (
                {
                    "offset": end,
                    "device": stat.st_dev,
                    "inode": stat.st_ino,
                    "mtime_ns": stat.st_mtime_ns,  # diagnostic only; never decides a skip
                    "fingerprint": _fingerprint(path, end),
                    "chunks": _extend_chunks(path, _chunks_of(old, start), start, end),
                    "account": account,
                    # Carry the audit walk across commits, so a transcript that
                    # grows every scan still has its older chunks re-read.
                    **{k: old[k] for k in _AUDIT_FIELDS if valid and k in old},
                },
                required,
            )
        except (OSError, ValueError, TypeError):
            logging.warning("Cowork transcript scan unavailable")
    batch.blocked = [
        {"provider_id": "anthropic", "credential_origin": origin, "reason": "events_untagged"}
        for origin in sorted(blocked_origins)
    ]
    return batch
