"""Manifest-scoped macOS Cowork tokens; never read audit logs or quota history."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.sidecar_pkg.event_extractors.anthropic import parse_anthropic_events
from scripts.sidecar_pkg.event_watermark import transcript_fingerprint as _fingerprint
from scripts.sidecar_pkg.identity import canonical_account_id
from scripts.sidecar_pkg.transcript_audit import (
    AUDIT_BYTES,
    AUDIT_FIELDS,
    audit,
    chunks_of,
    extend_chunks,
)


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


def _same_file(cursor: dict[str, Any], stat: os.stat_result) -> bool:
    return cursor.get("device") == stat.st_dev and cursor.get("inode") == stat.st_ino


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
    audit_bytes: int = AUDIT_BYTES,
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
        _save_audit(state_path, *audit(state, tracked, audit_bytes, _same_file))
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
                    "chunks": extend_chunks(path, chunks_of(old, start), start, end),
                    "account": account,
                    # Carry the audit walk across commits, so a transcript that
                    # grows every scan still has its older chunks re-read.
                    **{k: old[k] for k in AUDIT_FIELDS if valid and k in old},
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
