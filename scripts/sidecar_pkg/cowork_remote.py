"""Ids of remote Cowork sessions this Mac ran, so the server can read their usage from the cloud.

A remote Cowork session runs its model in Anthropic's cloud; the Mac's Cowork VM only runs its shell
commands, so no local transcript exists. Anthropic's session lists do not include these sessions,
but each one can be fetched by id. Claude Desktop records every remote Cowork session run against
this Mac in ``local-agent-mode-sessions/<accountUuid>/<orgUuid>/remote-session-spaces.json``::

    {"entries": [{"sessionId": "session_01<22 base62>", "folders": [...], ...}]}

Only the session id and the two directory UUIDs leave the machine: never the folders or any other
field. The server reads the usage with the account's own Claude login.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

FILE_NAME = "remote-session-spaces.json"
MAX_HINTS = 500
_MAX_BYTES = 1024 * 1024
_SESSION = re.compile(r"session_(01[A-Za-z0-9]{22})")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _entries(path: Path) -> list[Any]:
    try:
        if path.stat().st_size > _MAX_BYTES:
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = data.get("entries") if isinstance(data, dict) else None
    return entries if isinstance(entries, list) else []


def collect_remote_session_hints(root: Path) -> list[dict[str, str]]:
    """``[{"session_id": "cse_…", "account_uuid", "org_uuid"}]`` for every recorded session.

    A missing, oversized or unparsable file contributes nothing; malformed ids and directories that
    are not UUIDs are skipped.
    """
    hints: list[dict[str, str]] = []
    seen: set[str] = set()
    try:
        paths = sorted(root.glob(f"*/*/{FILE_NAME}"))
    except OSError:
        return []
    for path in paths:
        account_uuid, org_uuid = path.parent.parent.name.lower(), path.parent.name.lower()
        if not (_UUID.fullmatch(account_uuid) and _UUID.fullmatch(org_uuid)):
            continue
        for entry in _entries(path):
            raw = entry.get("sessionId") if isinstance(entry, dict) else None
            match = _SESSION.fullmatch(raw) if isinstance(raw, str) else None
            if match is None:
                continue
            session_id = f"cse_{match.group(1)}"
            if session_id in seen:
                continue
            seen.add(session_id)
            hints.append(
                {"session_id": session_id, "account_uuid": account_uuid, "org_uuid": org_uuid}
            )
            if len(hints) >= MAX_HINTS:
                logging.debug("remote Cowork: more than %d sessions, rest skipped", MAX_HINTS)
                return hints
    return hints
