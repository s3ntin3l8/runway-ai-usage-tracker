"""The Home banners read the credential inventory; they must raise exactly what Token Health did.

The banner rule is: a credential that is expired, expiring or rejected, and not ``redundant``
(an unrefreshable dead credential another healthy one can stand in for). Both views compute it
from the same rules; these tests run both on one real-DB world and compare the banner sets, so a
divergence (a ``redundant`` that only one view has, a disabled source that still alarms) fails
here instead of changing what the dashboard shouts about.
"""

from __future__ import annotations

import base64
import json
import time

import pytest
from sqlmodel.orm.session import Session

from app.models.db import CredentialSource
from app.services import credential_inventory
from app.services import token_health as th
from app.services.account_identity import canonical_account_id
from app.services.token_health import TokenHealthService
from tests.unit.token_health_world import build_world

BANNER_STATUSES = {"expired", "expiring", "invalid"}
CAROL = "carol@example.com"
DAVE = "dave@example.com"


def _jwt(expires_in: float) -> str:
    def b64(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    return f"{b64({'alg': 'none'})}.{b64({'exp': int(time.time() + expires_in)})}.sig"


async def _add_login_source(provider: str, account: str, sid: str, expires_in: float) -> None:
    source_id = f"sidecar:{sid}"
    with Session(th.engine) as s:
        s.add(
            CredentialSource(
                provider_id=provider,
                account_id=account,
                source_id=source_id,
                source_type="file",
                source_label="creds.json",
                credential_origin=f"path:/{sid}",
                sidecar_id="dev-01",
            )
        )
        s.commit()
    await th.token_cache.store(
        provider,
        {"oauth_token": _jwt(expires_in)},
        account_id=account,
        source_id=source_id,
        source="dev-01",
        source_metadata={"sidecar_id": "dev-01"},
    )


async def _world(monkeypatch) -> None:
    await build_world(monkeypatch)
    # An expired, unrefreshable login with a healthy sibling on the same account (redundant)...
    await _add_login_source("gemini", CAROL, "c1", -3600)
    await _add_login_source("gemini", CAROL, "c2", 7200)
    # ...and one with nothing to fall back on (a real alarm).
    await _add_login_source("gemini", DAVE, "d1", -3600)
    monkeypatch.setattr(credential_inventory, "engine", th.engine)
    monkeypatch.setattr(credential_inventory, "token_cache", th.token_cache)
    monkeypatch.setattr(credential_inventory, "_scan_server_credentials", lambda: ({}, set()))


def _account(provider_account: str) -> str:
    return canonical_account_id(th._underlying_account(provider_account))


async def _banner_sets() -> tuple[set, set]:
    health = {
        (r["provider"], _account(r["account_id"]), r["status"])
        for r in await TokenHealthService().get_health()
        if r["status"] in BANNER_STATUSES
        and not r["redundant"]
        # A cache-only pending Claude bundle has no durable source, so the inventory (a view
        # of sources) has no row for it; it is only ever waiting to be mapped.
        and not r.get("assignment_pending")
    }
    inv = await credential_inventory.build_inventory()
    inventory = {
        (s.provider_id, _account(s.account_id), s.status)
        for p in inv.providers
        for a in p.accounts
        for s in a.sources
        if s.status in BANNER_STATUSES and not s.redundant and s.enabled and not s.unused_reason
    }
    return health, inventory


@pytest.mark.asyncio
async def test_the_inventory_raises_the_same_banners_as_token_health(monkeypatch):
    await _world(monkeypatch)

    health, inventory = await _banner_sets()

    # The inventory is a view of *sources*. The world's one collector-refreshed aggregate (a
    # ChatGPT entry stored with no source id, which ingest never produces) has none, so only
    # Token Health lists it. Pinning the difference keeps any other divergence loud.
    assert health - inventory == {("chatgpt", "alice@example.com", "expired")}
    assert inventory - health == set()
    # Agreement must not be vacuous: a real alarm, a rejection, and a redundancy suppression.
    assert ("gemini", DAVE, "expired") in inventory  # nothing to fall back on
    assert ("openrouter", "bob@example.com", "invalid") in inventory  # the provider rejected it
    assert ("gemini", CAROL, "expired") not in inventory  # redundant: c2 still works


@pytest.mark.asyncio
async def test_redundant_and_rejected_are_exposed_on_the_inventory(monkeypatch):
    await _world(monkeypatch)

    inv = await credential_inventory.build_inventory()
    by_source = {s.source_id: s for p in inv.providers for a in p.accounts for s in a.sources}

    assert by_source["sidecar:c1"].redundant is True
    assert by_source["sidecar:c2"].redundant is False
    assert by_source["sidecar:d1"].redundant is False
    assert by_source["sidecar:o1"].rejected is True
    assert by_source["sidecar:g1"].rejected is False


@pytest.mark.asyncio
async def test_a_rejected_server_credential_is_invalid_and_flagged_in_both_views(monkeypatch):
    from app.services import auth_failures

    await _world(monkeypatch)
    auth_failures.mark("github", "default")  # the provider rejected the server's own token
    origin = {
        "source_type": "env",
        "label": "GITHUB_TOKEN",
        "keys": ["api_key"],
        "managed": False,
        "cli_owned": False,
        "shadowed": False,
        "exp": None,
        "rollable": False,
    }
    monkeypatch.setattr(
        credential_inventory, "_scan_server_credentials", lambda: ({"github": [origin]}, {"github"})
    )

    health, inventory = await _banner_sets()

    assert ("github", "default", "invalid") in health
    assert ("github", "default", "invalid") in inventory
    inv = await credential_inventory.build_inventory()
    (server_view,) = [
        s for p in inv.providers for a in p.accounts for s in a.sources if s.origin_kind == "server"
    ]
    assert server_view.rejected is True


@pytest.mark.asyncio
async def test_a_pasted_key_with_no_expiry_never_makes_a_dead_login_redundant(monkeypatch):
    """A config key or env var is only *assumed* valid, so it must not silence the alarm for an
    expired login on the same account, in either view."""
    await _world(monkeypatch)
    source_id = f"config:gemini:{DAVE}"
    with Session(th.engine) as s:
        s.add(
            CredentialSource(
                provider_id="gemini",
                account_id=DAVE,
                source_id=source_id,
                source_type="config",
                source_label="Manual configuration",
            )
        )
        s.commit()
    await th.token_cache.store(
        "gemini",
        {"api_key": "pasted-key"},  # pragma: allowlist secret
        account_id=DAVE,
        source="config",
        source_id=source_id,
        source_metadata={"source_type": "config"},
    )

    health, inventory = await _banner_sets()

    assert ("gemini", DAVE, "expired") in health
    assert ("gemini", DAVE, "expired") in inventory
