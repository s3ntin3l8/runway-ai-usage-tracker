"""Codex/ChatGPT auth.json must forward refresh_token + id_token (Option A).

The bug: the sidecar (and the server's registry-driven extraction) only mapped
`tokens.access_token` from `~/.codex/auth.json`, dropping `tokens.refresh_token`
and `tokens.id_token`. Without the refresh_token the auto-refresher can never
roll the access token (Token Health gets stuck "expired"); without the id_token
the cache derives a *new* hashed account_id on every CLI refresh, orphaning the
old entry. Both are fixed by forwarding all three token fields.
"""

import json
import sys
from pathlib import Path

import pytest

from app.services import credential_provider
from app.services.credential_provider import CredentialProvider

REGISTRY_JSON = Path(__file__).resolve().parents[2] / "app" / "core" / "registry.json"


def _codex_file_mapping(providers: dict) -> dict:
    """Pull the `mapping` of the chatgpt rule that reads ~/.codex/auth.json."""
    for rule in providers["chatgpt"]["rules"]:
        paths = rule.get("paths", [])
        if rule.get("type") == "file" and any("codex" in p for p in paths):
            return rule["mapping"]
    raise AssertionError("no codex auth.json file rule found for chatgpt")


def test_registry_json_codex_rule_forwards_refresh_and_id_token():
    """The source-of-truth registry must map all three token fields."""
    reg = json.loads(REGISTRY_JSON.read_text())
    mapping = _codex_file_mapping(reg["providers"])
    assert mapping.get("tokens.access_token") == "oauth_token"
    assert mapping.get("tokens.refresh_token") == "refresh_token"
    assert mapping.get("tokens.id_token") == "id_token"


def test_sidecar_baked_registry_matches_registry_json_for_codex_tokens():
    """The sidecar's baked-in __REGISTRY__ must not drift on the token fields.

    The running sidecar uses its baked-in copy (no runtime fetch), so a mapping
    that exists only in registry.json never reaches the wire.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    import sidecar

    src = _codex_file_mapping(json.loads(REGISTRY_JSON.read_text())["providers"])
    baked = _codex_file_mapping(sidecar.__REGISTRY__["providers"])
    for token_key in ("tokens.access_token", "tokens.refresh_token", "tokens.id_token"):
        assert baked.get(token_key) == src.get(token_key), (
            f"sidecar baked registry drifted on {token_key}"
        )


@pytest.mark.asyncio
async def test_server_extracts_refresh_and_id_token_from_codex_auth(tmp_path, monkeypatch):
    """End-to-end: registry-driven extraction returns all three token fields."""
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": "AT-access",
                    "refresh_token": "RT-refresh",
                    "id_token": "IDT-identity",
                    "account_id": "acct-123",
                }
            }
        )
    )

    # Redirect the codex path to our temp file; everything else "doesn't exist".
    def fake_resolve(path_str: str) -> str:
        return str(auth) if "codex" in path_str else "/nonexistent/never.json"

    monkeypatch.setattr(credential_provider.registry, "resolve_path", fake_resolve)
    monkeypatch.delenv("CHATGPT_OAUTH_TOKEN", raising=False)

    creds = CredentialProvider.get_credentials("chatgpt")

    assert creds.get("oauth_token") == "AT-access"
    assert creds.get("refresh_token") == "RT-refresh"
    assert creds.get("id_token") == "IDT-identity"
