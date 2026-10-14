"""Read opencode v2's ``credential`` table as if it were the legacy ``auth.json``.

opencode v2 imports ``auth.json`` into its SQLite database once and never reads
the file again (it leaves it behind, stale). The sidecar's credential rules and
the xAI keep-alive were written against ``auth.json``, so on a v2 host they would
keep serving a dead copy. This module presents the table in the legacy shape
(``{integration_id: value}``) so every existing dotted mapping (``opencode-go.key``,
``xai.access``, ...) keeps resolving unchanged, with the same origin string.

Sidecar-only: ``registry.json`` has no ``sqlite`` rule type on purpose, so the
server can never evaluate it. The database is opened read-only and never written.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Files the legacy rules point at; the DB lives next to them.
_AUTH_DIR_NAMES = frozenset({"opencode", ".opencode"})
_DB_NAME = "opencode.db"

# What a missing / locked / non-sqlite / unresolvable DB path raises (``Path.as_uri``
# raises ValueError for a path it cannot make absolute).
_EXPECTED_DB_ERRORS = (OSError, sqlite3.Error, ValueError)


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)


def sibling_db(auth_path: Path) -> Path | None:
    """The ``opencode.db`` beside an opencode ``auth.json``, or None.

    Only paths that look like opencode's own data dir qualify, so unrelated
    ``auth.json`` files (Codex, Grok, ...) are never matched.
    """
    if auth_path.name != "auth.json" or auth_path.parent.name not in _AUTH_DIR_NAMES:
        return None
    db = auth_path.parent / _DB_NAME
    return db if db.exists() else None


def is_v2_credentials_db(db_path: Path) -> bool:
    """True when the DB is a migrated v2 one that owns credentials.

    ``session_v2`` is the v2 signal; opencode 1.19 already ships an empty
    ``credential`` table, so that table alone proves nothing.
    """
    try:
        conn = _connect_ro(db_path)
    except _EXPECTED_DB_ERRORS:
        return False
    try:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name IN ('session_v2', 'credential')"
            )
        }
        return names == {"session_v2", "credential"}
    except _EXPECTED_DB_ERRORS:
        return False
    finally:
        conn.close()


def v2_db_for_auth(auth_path: Path) -> Path | None:
    """The v2 database that supersedes ``auth_path``, or None on v1 / unrelated files."""
    db = sibling_db(auth_path)
    return db if db is not None and is_v2_credentials_db(db) else None


def _rows(db_path: Path) -> list[tuple[str, str, Any, int]]:
    """``(integration_id, value_json, active, time_updated)`` for every credential row."""
    conn = _connect_ro(db_path)
    try:
        return list(
            conn.execute(
                "SELECT integration_id, value, active, time_updated FROM credential "
                "WHERE integration_id IS NOT NULL"
            )
        )
    finally:
        conn.close()


def _pick(rows: list[tuple[str, Any, int]]) -> tuple[dict[str, Any] | None, bool]:
    """Choose the credential for one integration: ``(value, ambiguous)``.

    Prefer the ``active`` row (``active`` is truthy: NULL and 0 both mean inactive;
    migrated rows leave it NULL); with none flagged a single row is unambiguous, while several are resolved by recency but reported
    ambiguous so callers can refuse to guess which account they belong to.
    """
    parsed: list[tuple[dict[str, Any], Any, int]] = []
    for raw, active, updated in rows:
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            parsed.append((value, active, updated or 0))
    if not parsed:
        return None, False
    flagged = [p for p in parsed if p[1]]
    pool = flagged or parsed
    pool.sort(key=lambda p: p[2], reverse=True)
    ambiguous = len(pool) > 1 and len({json.dumps(p[0], sort_keys=True) for p in pool}) > 1
    return pool[0][0], ambiguous


def load_auth_view(db_path: Path) -> dict[str, Any] | None:
    """Legacy ``auth.json``-shaped view of the credential table, or None if unreadable.

    ``type: "key"`` values keep their ``key``; ``type: "oauth"`` keep
    ``access`` / ``refresh`` / ``expires`` — the field names the rules already use.
    """
    try:
        rows = _rows(db_path)
    except _EXPECTED_DB_ERRORS:
        logger.debug("opencode credential table unreadable: %s", db_path, exc_info=True)
        return None
    grouped: dict[str, list[tuple[str, Any, int]]] = {}
    for integration_id, value, active, updated in rows:
        grouped.setdefault(integration_id, []).append((value, active, updated))
    view: dict[str, Any] = {}
    for integration_id, group in grouped.items():
        value, _ = _pick(group)
        if value is not None:
            view[integration_id] = value
    return view


def key_binding(db_path: Path, service: str, discovered_key: str) -> str:
    """Mirror of the ``account.json`` gate, driven by the credential table.

    ``"single"``: the discovered key is ``service``'s one active credential.
    ``"ambiguous"``: the table disagrees (a different active key) or holds several
    unresolved candidates, so assigning the key to a configured account is a guess.
    ``"unknown"``: nothing to compare against.
    """
    try:
        group = [(v, a, u) for i, v, a, u in _rows(db_path) if i == service]
    except _EXPECTED_DB_ERRORS:
        return "unknown"
    value, ambiguous = _pick(group)
    active_key = str((value or {}).get("key") or "").strip()
    if not active_key:
        return "unknown"
    if ambiguous or active_key != discovered_key.strip():
        return "ambiguous"
    return "single"
