"""Parse Antigravity CLI conversation SQLite databases into UsageEventPush records.

Each conversation is stored as ``~/.gemini/antigravity-cli/conversations/<uuid>.db``.
The ``gen_metadata`` table holds one row per assistant turn; each row carries a
protobuf blob with token counts but *no* wall-clock timestamp.

Field mapping (confirmed by MITM + offline decode, 2026-06-18):

    root.1.4.2  → tokens_input  (per-turn prompt tokens)
    root.1.4.3  → tokens_output (per-turn completion tokens)
    root.1.4.1  → tokens_cache_read (cached context reused this turn)
    root.1.4.5  → cumulative total — do NOT use as per-turn count
    root.1.19   → model id string (e.g. ``gemini-pro-default``, ``gemini-3-flash-a``)
    root.1.20   → repeated key-value metadata (``used_claude``, ``used_claude_conservative``)
    root.1.21   → human-readable display name (``Gemini 3.1 Pro (High)``)

Workspace path (cwd) comes from the sibling ``trajectory_metadata_blob`` table,
field 7 (a ``file://`` URI).

Since there is no per-row timestamp, the DB file modification time is used as an
approximate event timestamp. Events are deduplicated server-side by their stable
event_id (``<conversation_id>|gen_<idx>``).
"""

import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.models.schemas import UsageEventPush  # noqa: E402

# ── Dependency-free protobuf varint reader ────────────────────────────────────


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result, shift = 0, 0
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return result, pos


def _parse_proto_fields(buf: bytes) -> dict[int, list]:
    """Parse a single protobuf message into ``{field_num: [values]}``."""
    fields: dict[int, list] = {}
    pos = 0
    while pos < len(buf):
        try:
            tag, pos = _read_varint(buf, pos)
        except Exception:
            break
        field_num = tag >> 3
        wire_type = tag & 0x7
        if field_num == 0:
            break
        if wire_type == 0:
            val, pos = _read_varint(buf, pos)
            fields.setdefault(field_num, []).append(val)
        elif wire_type == 1:
            if pos + 8 > len(buf):
                break
            pos += 8
        elif wire_type == 2:
            length, pos = _read_varint(buf, pos)
            if pos + length > len(buf):
                break
            fields.setdefault(field_num, []).append(buf[pos : pos + length])
            pos += length
        elif wire_type == 5:
            if pos + 4 > len(buf):
                break
            pos += 4
        else:
            break
    return fields


def _first_int(fields: dict, field_num: int, default: int = 0) -> int:
    vals = fields.get(field_num, [])
    for v in vals:
        if isinstance(v, int):
            return v
    return default


def _first_str(fields: dict, field_num: int, default: str = "") -> str:
    vals = fields.get(field_num, [])
    for v in vals:
        if isinstance(v, (bytes, bytearray)):
            try:
                return v.decode("utf-8", errors="replace")
            except Exception:
                pass
    return default


# ── KV metadata parser (field 20 repeated pairs) ─────────────────────────────


def _parse_kv_metadata(fields: dict) -> dict[str, str]:
    """Extract the repeated key-value pairs from field 20 of the root.1 message."""
    result: dict[str, str] = {}
    for blob in fields.get(20, []):
        if not isinstance(blob, (bytes, bytearray)):
            continue
        kv = _parse_proto_fields(bytes(blob))
        key = _first_str(kv, 1)
        value = _first_str(kv, 2)
        if key:
            result[key] = value
    return result


# ── Model normalizer ──────────────────────────────────────────────────────────


def _normalize_ag_model(raw_model: str, display_name: str, kv: dict[str, str]) -> str:
    """Map the raw agy model string to a stable cost-bucket id.

    When Claude is selected (``used_claude=true``), fall back to a family name
    so the cost_calculator can match against the Anthropic pricing rows.
    The display name (f1.21, e.g. "Gemini 3.1 Pro (High)") carries version info
    when the raw model id is a generic placeholder like ``gemini-pro-default``.
    """
    if kv.get("used_claude_conservative") == "true":
        return "claude-opus"
    if kv.get("used_claude") == "true":
        return "claude-sonnet"

    # Prefer the display name for version detection when raw_model is generic.
    name_for_version = display_name or raw_model or ""
    has_3x = any("gemini-3" in (raw_model or "").lower() for _ in [1]) or (
        "3." in name_for_version or " 3 " in name_for_version
    )

    lower = (raw_model or "").lower()
    if not lower:
        return "unknown"
    if "flash" in lower:
        if "lite" in lower:
            return "flash-lite-3" if has_3x else "flash-lite"
        return "flash-3" if has_3x else "flash"
    if "pro" in lower:
        return "pro-3" if has_3x else "pro"
    return raw_model or "unknown"


# ── cwd extractor from trajectory_metadata_blob ───────────────────────────────


def _read_cwd_from_db(conn: sqlite3.Connection) -> str | None:
    """Return the workspace path from the trajectory_metadata_blob table, or None."""
    try:
        cur = conn.cursor()
        cur.execute("SELECT data FROM trajectory_metadata_blob LIMIT 1")
        row = cur.fetchone()
        if not row:
            return None
        blob = bytes(row[0])
        tmb_fields = _parse_proto_fields(blob)
        # Field 7 = workspace root as a file:// URI
        uri = _first_str(tmb_fields, 7)
        if uri.startswith("file://"):
            return uri[7:]  # strip scheme prefix
        if uri:
            return uri
    except Exception:
        pass
    return None


# ── Main entry point ──────────────────────────────────────────────────────────


def parse_antigravity_events(
    db_paths: list[Path],
    account_id: str,
    since: datetime,
) -> list[UsageEventPush]:
    """Extract UsageEventPush records from Antigravity conversation databases.

    Processes only DBs whose file modification time is strictly after ``since``.
    All rows in each qualifying DB are emitted (server deduplicates by event_id).
    The event timestamp is approximated from the DB file modification time.

    Args:
        db_paths: List of paths to ``*.db`` conversation files.
        account_id: Canonical account email or "default".
        since: Only process DBs modified strictly after this datetime.
    """
    since_ts = since.timestamp()
    events: list[UsageEventPush] = []

    for db_path in db_paths:
        try:
            mtime = os.path.getmtime(db_path)
        except OSError:
            continue
        if mtime <= since_ts:
            continue

        conversation_id = db_path.stem

        try:
            conn = sqlite3.connect(str(db_path), timeout=5)
        except Exception:
            continue

        try:
            cwd = _read_cwd_from_db(conn)

            cur = conn.cursor()
            try:
                cur.execute("SELECT idx, data FROM gen_metadata ORDER BY idx ASC")
                rows = cur.fetchall()
            except Exception:
                continue
        finally:
            conn.close()

        for row_idx, (idx, blob) in enumerate(rows):
            if not blob:
                continue
            try:
                root = _parse_proto_fields(bytes(blob))
            except Exception:
                continue

            f1_blobs = root.get(1, [])
            if not f1_blobs:
                continue

            f1_bytes = f1_blobs[0]
            if not isinstance(f1_bytes, (bytes, bytearray)):
                continue

            f1 = _parse_proto_fields(bytes(f1_bytes))

            # Usage stats are nested inside field 4.
            f4_blobs = f1.get(4, [])
            if not f4_blobs:
                continue
            f4 = _parse_proto_fields(bytes(f4_blobs[0]))

            tokens_input = _first_int(f4, 2)
            tokens_output = _first_int(f4, 3)
            tokens_cache_read = _first_int(f4, 1)
            # Field 5 is a monotonic cumulative — skip it.

            # Skip rows with no token data (non-assistant turns / empty metadata).
            if tokens_input == 0 and tokens_output == 0:
                continue

            raw_model = _first_str(f1, 19, "unknown")
            display_name = _first_str(f1, 21, "")
            kv = _parse_kv_metadata(f1)
            model_id = _normalize_ag_model(raw_model, display_name, kv)

            event_id = f"{conversation_id}|gen_{idx}"

            # Spread events across the same second by microsecond offset so
            # the server's time-ordering is stable within one DB.
            event_ts = datetime.fromtimestamp(mtime + row_idx * 0.001, tz=UTC)

            events.append(
                UsageEventPush(
                    provider_id="antigravity",
                    account_id=account_id,
                    event_id=event_id,
                    ts=event_ts.isoformat(),
                    model_id=model_id,
                    session_id=conversation_id,
                    cwd=cwd,
                    tokens_input=tokens_input,
                    tokens_output=tokens_output,
                    tokens_cache_read=tokens_cache_read,
                    tokens_cache_create=0,
                    tokens_reasoning=0,
                    stop_reason=None,
                    tool_calls=0,
                    cost_usd=None,
                )
            )

    return events
