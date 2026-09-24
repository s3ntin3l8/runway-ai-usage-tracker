"""Persistent watermark of last-pushed event timestamp per (provider, account)."""

import json
from datetime import UTC, datetime
from pathlib import Path

from scripts.sidecar_pkg.identity import canonical_account_id


def _key(provider_id: str, account_id: str) -> str:
    return f"{provider_id}|{canonical_account_id(account_id)}"


class EventWatermark:
    """Track last-successfully-pushed event timestamp per (provider_id, account_id).

    Stored in a JSON file at `path`. Thread-safe for single-process use (GIL + atomic
    write pattern via write-to-tmp then rename is not needed here; the sidecar is
    single-threaded per cycle).
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._data: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        try:
            text = self.path.read_text()
            raw = json.loads(text).get("last_pushed_ts", {})
        except (FileNotFoundError, json.JSONDecodeError):
            raw = {}
        # Fold keys written before account ids were canonicalized (e.g.
        # ``anthropic|Alice@X.com``) onto the canonical key, keeping the
        # latest timestamp — otherwise the first cycle after an upgrade
        # would re-extract the whole bootstrap window.
        self._data = {}
        for key, ts in raw.items():
            provider_id, sep, account_id = key.partition("|")
            ckey = _key(provider_id, account_id) if sep else key
            if ckey not in self._data or _parse(ts) > _parse(self._data[ckey]):
                self._data[ckey] = ts

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"last_pushed_ts": self._data}))

    def last_pushed(self, provider_id: str, account_id: str) -> datetime | None:
        """Return the last pushed ts for (provider_id, account_id), or None."""
        v = self._data.get(_key(provider_id, account_id))
        if not v:
            return None
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None

    def advance(self, provider_id: str, account_id: str, ts: datetime) -> None:
        """Move the watermark forward to ts (no-op if ts is not later than current)."""
        key = _key(provider_id, account_id)
        cur = self.last_pushed(provider_id, account_id)
        if cur is None or ts > cur:
            self._data[key] = ts.isoformat()
            self._save()


def _parse(ts: str) -> datetime:
    """Parse a stored watermark ts for comparison; unparseable sorts oldest."""
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
