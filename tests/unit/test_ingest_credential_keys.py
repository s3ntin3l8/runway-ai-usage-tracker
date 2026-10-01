"""Every credential key the sidecar can emit must survive server-side ingest.

The ingest whitelist silently drops unknown token-card keys, which disables the
collector that reads them. This pins ``_INGEST_CREDENTIAL_KEYS`` to the mapping
targets declared in the sidecar's embedded registry.
"""

import re
from pathlib import Path

from app.api.endpoints.fleet import _INGEST_CREDENTIAL_KEYS, _is_cookie_key

SIDECAR = Path(__file__).resolve().parents[2] / "scripts" / "sidecar.py"

# Mapping targets that are identity metadata or have no server-side consumer.
_NOT_CREDENTIALS = {
    "name",  # account label hint, consumed by the sidecar's identity resolver
    "account_id",
    "account_label",
    "http_referer",  # OpenRouter attribution headers — not secrets, no collector reads them
    "x_title",
}


def _sidecar_mapping_targets() -> set[str]:
    text = SIDECAR.read_text()
    return set(re.findall(r'"mapping":\s*\{\s*"[A-Za-z_]+":\s*"([A-Za-z_\-\.]+)"', text))


def test_sidecar_mapping_targets_survive_ingest() -> None:
    targets = _sidecar_mapping_targets()
    assert targets, "failed to parse the sidecar registry mappings"
    dropped = {
        t
        for t in targets - _NOT_CREDENTIALS
        if t not in _INGEST_CREDENTIAL_KEYS and not _is_cookie_key(t)
    }
    assert not dropped, f"sidecar emits keys the server ingest drops: {sorted(dropped)}"


def test_bundled_cookie_keys_are_cookie_family() -> None:
    assert _is_cookie_key("session_cookie")
    assert _is_cookie_key("console_session")
    assert _is_cookie_key("cookie_sessionKey")
    assert not _is_cookie_key("api_key")
