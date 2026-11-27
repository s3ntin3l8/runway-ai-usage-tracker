"""Persistent event timestamps and Claude CLI transcript positions."""

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.sidecar_pkg.identity import canonical_account_id


def _key(provider_id: str, account_id: str) -> str:
    return f"{provider_id}|{canonical_account_id(account_id)}"


class EventWatermark:
    """Track last-successfully-pushed event timestamp per (provider_id, account_id).

    Stored in a JSON file at `path`. The sidecar uses one writer per cycle.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._data: dict[str, str] = {}
        self._transcripts: dict[str, dict[str, Any]] = {}
        self._pending_transcripts: dict[str, dict[str, Any]] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        try:
            text = self.path.read_text()
            data = json.loads(text)
            raw = data.get("last_pushed_ts", {})
            transcripts = data.get("anthropic_transcripts")
            if isinstance(transcripts, dict):
                self._transcripts = transcripts
        except (FileNotFoundError, json.JSONDecodeError, AttributeError):
            raw = {}
        # Fold keys written before account ids were canonicalized (e.g.
        # ``anthropic|Alice@X.com``) onto the canonical key, keeping the
        # latest timestamp — otherwise the first cycle after an upgrade
        # would re-extract the whole bootstrap window.
        self._data = {}
        for key, ts in raw.items() if isinstance(raw, dict) else ():
            provider_id, sep, account_id = key.partition("|")
            ckey = _key(provider_id, account_id) if sep else key
            if ckey not in self._data or _parse(ts) > _parse(self._data[ckey]):
                self._data[ckey] = ts

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        file = tempfile.NamedTemporaryFile(
            "w", dir=self.path.parent, prefix=".event-watermark-", delete=False
        )
        tmp = Path(file.name)
        try:
            with file:
                json.dump(
                    {"last_pushed_ts": self._data, "anthropic_transcripts": self._transcripts},
                    file,
                )
            os.replace(tmp, self.path)
        finally:
            tmp.unlink(missing_ok=True)

    def anthropic_transcript(self, path: Path, account_id: str) -> dict[str, Any]:
        cursor = self._transcripts.get(str(path), {})
        return cursor if isinstance(cursor, dict) and cursor.get("account") == account_id else {}

    def stage_anthropic_transcripts(self, cursors: dict[str, dict[str, Any]]) -> None:
        self._pending_transcripts.update(cursors)

    def commit(self) -> None:
        """Save the accepted cycle's timestamps and scan positions together."""
        if self._pending_transcripts:
            self._transcripts.update(self._pending_transcripts)
            self._dirty = True
        if self._dirty:
            self._save()
            self._dirty = False
            self._pending_transcripts.clear()

    def last_pushed(self, provider_id: str, account_id: str) -> datetime | None:
        """Return the last pushed ts for (provider_id, account_id), or None."""
        v = self._data.get(_key(provider_id, account_id))
        if not v:
            return None
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None

    def advance(
        self, provider_id: str, account_id: str, ts: datetime, *, persist: bool = True
    ) -> None:
        """Move the watermark forward to ts (no-op if ts is not later than current)."""
        key = _key(provider_id, account_id)
        cur = self.last_pushed(provider_id, account_id)
        if cur is None or ts > cur:
            self._data[key] = ts.isoformat()
            self._dirty = True
        if persist and self._dirty:
            self._save()
            self._dirty = False


def _parse(ts: str) -> datetime:
    """Parse a stored watermark ts for comparison; unparseable sorts oldest."""
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


_HEAD_BYTES = 4 * 1024
_TAIL_BYTES = 64 * 1024


def transcript_fingerprint(path: Path, end: int) -> str:
    """Digest a committed region: all of it while it fits the window, else head and tail.

    The committed region is re-checked on every scan, so hashing it whole
    would re-read the whole transcript history each cycle and leave the
    per-cycle byte budget. A region up to the window size is hashed in full;
    a larger one contributes only its head and its tail, which is where a
    rewrite lands: Claude Code rewrites a transcript in place, and the bytes
    that change sit at the end of the file. The middle of a larger region is
    not covered here. The Cowork reader closes that gap with per-chunk
    digests and a rolling audit (``cowork_events._audit``); the CLI
    transcript reader still relies on this window alone (#1407).
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
