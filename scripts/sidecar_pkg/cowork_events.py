"""Manifest-scoped macOS Cowork tokens; never read audit logs or quota history."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.sidecar_pkg.event_extractors.anthropic import parse_anthropic_events
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


_HEAD_BYTES = 4 * 1024
_TAIL_BYTES = 64 * 1024


def _fingerprint(path: Path, end: int) -> str:
    """Digest a committed region: all of it while it fits the window, else head and tail.

    The committed region is re-checked on every scan, so hashing it whole
    would re-read the whole transcript history each cycle and leave the
    per-cycle byte budget. A region up to the window size is hashed in full;
    a larger one contributes only its head and its tail, which is where a
    rewrite lands: Claude Code rewrites a transcript in place, and the bytes
    that change sit at the end of the file. The middle of a larger region is
    not covered; that gap is tracked in #802.
    """
    head = min(end, _HEAD_BYTES)
    tail_start = max(head, end - _TAIL_BYTES)
    digest = hashlib.sha256()
    digest.update(head.to_bytes(8, "big"))
    digest.update((end - tail_start).to_bytes(8, "big"))
    with path.open("rb") as f:
        block = f.read(head)
        if block:
            digest.update(block)
        if end > head:
            f.seek(tail_start)
            remaining = end - tail_start
            while remaining > 0:
                chunk = f.read(min(remaining, 1024 * 1024))
                if not chunk:
                    break
                digest.update(chunk)
                remaining -= len(chunk)
    return digest.hexdigest()


def _within(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


@dataclass
class CoworkBatch:
    """A scan and its uncommitted checkpoints, owned only at transport boundaries."""

    events: list[dict[str, Any]]
    blocked: list[dict[str, str]]
    state_path: Path
    checkpoints: dict[str, tuple[dict[str, Any], set[str]]] = field(default_factory=dict)
    owned: set[str] = field(default_factory=set)

    def mark_owned(self, events: list[dict[str, Any]]) -> None:
        self.owned.update(e["event_id"] for e in events if e.get("provider_id") == "anthropic")
        ready = {k: v[0] for k, v in self.checkpoints.items() if v[1] <= self.owned}
        if not ready:
            return
        data = _load(self.state_path)
        data.update(ready)
        self.state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(dir=self.state_path.parent, prefix=".cowork-")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"version": 1, "files": data}, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(name, self.state_path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        for k in ready:
            del self.checkpoints[k]


def collect_cowork(
    base: Path,
    state_path: Path,
    tag_hints: dict[str, str],
    *,
    max_bytes: int = 4 * 1024 * 1024,
    max_events: int = 500,
    existing_ids: set[str] | None = None,
) -> CoworkBatch:
    """Backfill complete lines independently of CLI timestamps, oldest files first."""
    batch = CoworkBatch([], [], state_path)
    state = _load(state_path)
    seen_paths: set[Path] = set()
    seen_ids = set(existing_ids or ())
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
            required: set[str] = set()
            for event in parsed:
                required.add(event.event_id)
                if event.event_id in seen_ids:
                    continue
                seen_ids.add(event.event_id)
                payload = event.model_dump(mode="json")
                payload["account_source"] = source
                payload["credential_origin"] = origin
                batch.events.append(payload)
            if source == "default" and required:
                blocked_origins.add(origin)
            batch.checkpoints[str(path)] = (
                {
                    "offset": end,
                    "device": stat.st_dev,
                    "inode": stat.st_ino,
                    "mtime_ns": stat.st_mtime_ns,  # diagnostic only; never decides a skip
                    "fingerprint": _fingerprint(path, end),
                    "account": account,
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
