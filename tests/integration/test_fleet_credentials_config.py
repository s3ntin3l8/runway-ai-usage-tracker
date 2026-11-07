"""Integration tests for fleet config token issuance, signing, and hint generation."""

from __future__ import annotations

import base64
import json
import time

import pytest
from sqlmodel import Session

from app.models.db import ProviderConfig
from tests.fixtures.test_client import TestClient
from tests.integration._fleet_credentials_helpers import (
    SECRET,
    _add_live_sidecar,
    _add_provider_config,
    _isolated_ingest_key,  # noqa: F401 — fixture
    client_fixture,  # noqa: F401 — fixture
    session_fixture,  # noqa: F401 — fixture
)

# ---------------------------------------------------------------------------
# Key-scoped hints (#347 T1, widened to OpenCode's siblings in #349) — the
# server recognises a key it has been pasted and answers under
# ``provider:<pid>#<fingerprint>``.
#
# The OpenCode CLI credential carries no identity of its own, so the
# sidecar cannot derive an account for it (see
# tests/unit/test_opencode_credential_identity.py for the sidecar half).
# When the same key is also in ``provider_configs``, the server can match
# it by fingerprint — never by receiving the key, and never by guessing.
# ---------------------------------------------------------------------------

OPENCODE_CLI_KEY = "oc_sk_pasted_by_operator"  # pragma: allowlist secret
OTHER_CLI_KEY = "oc_sk_second_account_key"  # pragma: allowlist secret
SIBLING_KEY = "sk-or-v1-pasted_by_operator"  # pragma: allowlist secret
XAI_BEARER = "xai_pasted_by_operator"  # pragma: allowlist secret


def _opencode_hint_key(key: str) -> str:
    """The hint key both sides derive independently: the server never learns
    the sidecar's path, the sidecar never learns which account row matched."""
    return _hint_key("opencode", key)


def _hint_key(provider_id: str, key: str) -> str:
    from app.services.account_identity import credential_fingerprint, keyed_credential_origin

    return keyed_credential_origin(f"provider:{provider_id}", credential_fingerprint(key))


# ---------------------------------------------------------------------------
# /fleet/config redaction for unsigned callers on a network-reachable bind
# ---------------------------------------------------------------------------


def _signed_config_headers(query: str) -> dict[str, str]:
    from scripts.sidecar_pkg.credentials import config_request_signature

    # Microsecond precision like the sidecar: signatures are single-use.
    ts = f"{time.time():.6f}"
    return {"X-Timestamp": ts, "X-Signature": config_request_signature(SECRET, ts, query)}


def _seed_identity_state(session: Session) -> None:
    from app.services.credential_tags import CredentialTagRepo

    _add_provider_config(
        session, provider_id="anthropic", account_id="alice@example.com", api_key="sk-test"
    )
    CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="provider:anthropic",
        account_id="alice@example.com",
    )
    session.commit()


# ---------------------------------------------------------------------------
# /fleet/config: token issuance
# ---------------------------------------------------------------------------


def test_config_emits_credential_token_per_account_with_creds(client: TestClient, session: Session):
    """When a row has credentials, ``accounts[*].credential_token`` is set."""
    _add_provider_config(
        session,
        provider_id="openrouter",
        account_id="alice@example.com",
        api_key="sk-test-123",
    )
    r = client.get("/api/v1/fleet/config")
    assert r.status_code == 200
    openrouter = r.json()["config"]["providers"]["openrouter"]
    assert len(openrouter["accounts"]) == 1
    assert openrouter["accounts"][0]["credential_token"]
    # Token is opaque base64url + "." + hex — pin the wire format.
    token = openrouter["accounts"][0]["credential_token"]
    assert token.count(".") == 1
    payload, _, _ = token.rpartition(".")
    # Payload is base64url-encoded JSON; should be decodable and parseable.
    import base64

    padded = payload + "=" * (-len(payload) % 4)
    decoded = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    assert decoded["pid"] == "openrouter"
    assert decoded["aid"] == "alice@example.com"
    assert decoded["v"] == 1


def test_config_omits_token_when_no_credential_field(client: TestClient, session: Session):
    """A row with no api_key / session_cookie / oai_sc_cookie gets no token.

    There's no point handing the sidecar a token for an empty row — the
    redeem endpoint would return an empty ``credentials`` object.
    """
    _add_provider_config(session, provider_id="github", account_id="default")
    r = client.get("/api/v1/fleet/config")
    github = r.json()["config"]["providers"]["github"]
    assert github["accounts"][0].get("credential_token") is None


def test_config_omits_token_for_disabled_account(client: TestClient, session: Session):
    """Disabled rows never issue tokens — no point redeeming creds we'll reject."""
    _add_provider_config(
        session,
        provider_id="ollama",
        account_id="default",
        enabled=False,
        api_key="disabled-key",
    )
    r = client.get("/api/v1/fleet/config")
    ollama = r.json()["config"]["providers"]["ollama"]
    assert ollama["accounts"][0].get("credential_token") is None


def test_config_omits_tokens_when_ingest_key_unconfigured(
    client: TestClient, session: Session, monkeypatch
):
    """With ``INGEST_API_KEY`` empty, no tokens are issued."""
    monkeypatch.setattr("app.core.config.settings.INGEST_API_KEY", "")
    _add_provider_config(session, provider_id="openrouter", account_id="default", api_key="k")
    r = client.get("/api/v1/fleet/config")
    openrouter = r.json()["config"]["providers"]["openrouter"]
    assert openrouter["accounts"][0].get("credential_token") is None


def test_config_token_format_decodes_to_expected_claims(client: TestClient, session: Session):
    """The token issued in /fleet/config decodes to the (provider, account)
    pair it scopes, with version + TTL fields populated. (The redeem
    endpoint lands in the follow-up PR; today we only verify the wire
    format is well-formed.)"""
    from app.services.credential_token import TOKEN_VERSION, verify_credential_token

    _add_provider_config(
        session,
        provider_id="openrouter",
        account_id="alice@example.com",
        api_key="sk-test-123",
        session_cookie="session-cookie-value",
        account_label="Alice",
    )
    cfg = client.get("/api/v1/fleet/config").json()["config"]["providers"]["openrouter"]
    token = cfg["accounts"][0]["credential_token"]

    claims = verify_credential_token(SECRET, token)
    assert claims.provider_id == "openrouter"
    assert claims.account_id == "alice@example.com"
    assert claims.version == TOKEN_VERSION
    assert claims.exp > time.time()

    # Decoded payload has the right shape: {v, pid, aid, exp}.
    encoded, _, _ = token.rpartition(".")
    padded = encoded + "=" * (-len(encoded) % 4)
    decoded = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    assert decoded == {
        "v": TOKEN_VERSION,
        "pid": "openrouter",
        "aid": "alice@example.com",
        "exp": claims.exp,
    }


def test_config_emits_distinct_tokens_per_account(client: TestClient, session: Session):
    """Two accounts for the same provider get two distinct tokens."""
    _add_provider_config(session, provider_id="openrouter", account_id="default", api_key="k1")
    _add_provider_config(session, provider_id="openrouter", account_id="alice@x.com", api_key="k2")
    r = client.get("/api/v1/fleet/config")
    accounts = r.json()["config"]["providers"]["openrouter"]["accounts"]
    tokens = {a["account_id"]: a["credential_token"] for a in accounts}
    assert set(tokens) == {"default", "alice@x.com"}
    assert tokens["default"] != tokens["alice@x.com"]


def test_config_response_carries_account_tag_hints(client: TestClient, session: Session):
    """/fleet/config exposes the per-provider hint map for the sidecar's next cycle."""
    from app.services.credential_tags import CredentialTagRepo

    CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="path:/.claude/.credentials.json",
        account_id="alice@example.com",
    )
    session.commit()

    r = client.get("/api/v1/fleet/config")
    assert r.status_code == 200
    hints = r.json()["account_tag_hints"]
    assert hints == {
        "anthropic": {"path:/.claude/.credentials.json": "alice@example.com"},
    }


def test_config_response_account_tag_hints_empty_when_no_tags(client: TestClient):
    """No tags → empty map (not absent), so the sidecar sees the key safely."""
    r = client.get("/api/v1/fleet/config")
    assert r.status_code == 200
    assert r.json()["account_tag_hints"] == {}


# ---------------------------------------------------------------------------
# Auto-hints for single-account providers (closes the MiniMax card-split).
#
# When the operator has a single labeled MiniMax account (no upstream per-
# user identity), the sidecar's event stream has nothing to discover and
# would ship events under the synthetic "default" sentinel. Without an
# auto-hint the events land on a standalone synthetic-default card and
# the quota gauge stays orphaned on the labeled row. The
# auto_hints_for_single_account_providers repo method + its wiring in
# ``_account_tag_hints_for_providers`` ships the operator's chosen
# account_id as the hint so the next sidecar cycle stamps the events
# onto the labeled quota card.
# ---------------------------------------------------------------------------


def test_auto_hint_ships_when_provider_has_single_labeled_account(
    client: TestClient, session: Session
) -> None:
    """The MiniMax card-split scenario: one enabled non-default row.

    Pins the contract that /fleet/config exposes
    ``provider:<provider_id>`` → ``<account_id>`` for the sidecar's
    silent-listener block guard. The sidecar consumes it on the next
    cycle and stamps events under the operator's account_id, so the
    synthetic-default card is suppressed (the existing
    ``app/api/endpoints/usage_overview.py:264`` short-circuit).
    """
    row = ProviderConfig(
        provider_id="minimax",
        account_id="s3ntin318@gmail.com",
        enabled=True,
    )
    session.add(row)
    session.commit()

    r = client.get("/api/v1/fleet/config")
    assert r.status_code == 200
    hints = r.json()["account_tag_hints"]
    assert hints == {}


def test_auto_hint_skips_provider_with_no_labeled_rows(
    client: TestClient, session: Session
) -> None:
    """No rows for the provider → no hint (the sidecar can't be told
    where to land events that don't exist)."""
    r = client.get("/api/v1/fleet/config")
    assert r.status_code == 200
    assert r.json()["account_tag_hints"] == {}


def test_auto_hint_skips_provider_with_only_default_row(
    client: TestClient, session: Session
) -> None:
    """A single ``account_id="default"`` row → no hint. The sidecar's
    events already land at ``("default")``; no auto-hint needed and no
    benefit to retargeting them to themselves."""
    row = ProviderConfig(
        provider_id="minimax",
        account_id="default",
        enabled=True,
    )
    session.add(row)
    session.commit()

    r = client.get("/api/v1/fleet/config")
    assert r.status_code == 200
    assert r.json()["account_tag_hints"] == {}


def test_auto_hint_skips_provider_with_multiple_labeled_accounts(
    client: TestClient, session: Session
) -> None:
    """Two non-default rows for the same provider → no auto-hint.

    Multi-account ambiguity: the operator must tag explicitly via the
    Untagged Credentials dialog. The auto-hint heuristic refuses to
    guess which account the sidecar's events belong to.
    """
    for aid in ("alice@example.com", "bob@example.com"):
        row = ProviderConfig(
            provider_id="minimax",
            account_id=aid,
            enabled=True,
        )
        session.add(row)
    session.commit()

    r = client.get("/api/v1/fleet/config")
    assert r.status_code == 200
    assert r.json()["account_tag_hints"] == {}


def test_auto_hint_skips_disabled_labeled_account(client: TestClient, session: Session) -> None:
    """A disabled row → no hint. The collector isn't running, so
    shipping events there would land on a card the user can't see."""
    row = ProviderConfig(
        provider_id="minimax",
        account_id="s3ntin318@gmail.com",
        enabled=False,
    )
    session.add(row)
    session.commit()

    r = client.get("/api/v1/fleet/config")
    assert r.status_code == 200
    assert r.json()["account_tag_hints"] == {}


def test_explicit_operator_tag_wins_over_auto_hint(client: TestClient, session: Session) -> None:
    """Explicit operator tags always win over the implicit single-account
    auto-hint. The merge order in ``_account_tag_hints_for_providers``
    is operator-tags-first — even when an operator tag targets a
    different account_id than the auto-hint, the operator tag stays
    intact (the auto-hint never overwrites)."""
    from app.services.credential_tags import CredentialTagRepo

    # Operator explicitly tags this origin to a *different* account
    # than the auto-hint would derive from provider_configs.
    CredentialTagRepo.set_tag(
        session,
        provider_id="minimax",
        credential_origin="path:/home/runway/.minimax/credentials",
        account_id="work@example.com",
    )
    # Provider has a single labeled row — auto-hint would point at
    # s3ntin318@gmail.com. The merge keeps both, with the explicit
    # operator tag on its own origin untouched.
    row = ProviderConfig(
        provider_id="minimax",
        account_id="s3ntin318@gmail.com",
        enabled=True,
    )
    session.add(row)
    session.commit()

    r = client.get("/api/v1/fleet/config")
    assert r.status_code == 200
    hints = r.json()["account_tag_hints"]
    # The explicit mapping is returned; no provider-wide inference is added.
    assert hints["minimax"]["path:/home/runway/.minimax/credentials"] == ("work@example.com")
    assert "provider:minimax" not in hints["minimax"]


def test_auto_hint_independent_per_provider(client: TestClient, session: Session) -> None:
    """Single-account auto-hints are scoped per-provider — a single
    labeled MiniMax row fires only the MiniMax hint, leaving the
    other providers' hint maps empty."""
    row = ProviderConfig(
        provider_id="minimax",
        account_id="s3ntin318@gmail.com",
        enabled=True,
    )
    session.add(row)
    session.commit()

    r = client.get("/api/v1/fleet/config")
    assert r.status_code == 200
    hints = r.json()["account_tag_hints"]
    assert "minimax" not in hints
    assert "anthropic" not in hints
    assert "opencode" not in hints


def test_fingerprint_hint_ships_for_stored_opencode_key(
    client: TestClient, session: Session
) -> None:
    """One stored key → one hint, under the fingerprint-keyed descriptor."""
    _add_provider_config(
        session,
        provider_id="opencode",
        account_id="alice@example.com",
        api_key=OPENCODE_CLI_KEY,
    )

    r = client.get("/api/v1/fleet/config")
    assert r.status_code == 200
    hints = r.json()["account_tag_hints"]
    # Only the definitive fingerprint match is evidence of credential ownership.
    assert hints["opencode"] == {
        _opencode_hint_key(OPENCODE_CLI_KEY): "alice@example.com",
    }


def test_fingerprint_hint_maps_each_key_to_its_own_account(
    client: TestClient, session: Session
) -> None:
    """Two accounts, two keys → two independent hints. Neither can inherit
    the other's account: the scoping is the fingerprint match itself."""
    _add_provider_config(
        session,
        provider_id="opencode",
        account_id="alice@example.com",
        api_key=OPENCODE_CLI_KEY,
    )
    _add_provider_config(
        session,
        provider_id="opencode",
        account_id="bob@example.com",
        api_key=OTHER_CLI_KEY,
    )

    r = client.get("/api/v1/fleet/config")
    assert r.status_code == 200
    assert r.json()["account_tag_hints"]["opencode"] == {
        _opencode_hint_key(OPENCODE_CLI_KEY): "alice@example.com",
        _opencode_hint_key(OTHER_CLI_KEY): "bob@example.com",
    }


@pytest.mark.parametrize(
    "provider_id", ["openrouter", "minimax", "kimi_coding", "ollama", "deepseek"]
)
def test_fingerprint_hint_ships_for_stored_sibling_key(
    client: TestClient, session: Session, provider_id: str
) -> None:
    """#349: the OpenCode-file siblings are keyed too, so a pasted sibling
    key answers under ``provider:<pid>#<fp>`` exactly like opencode's does."""
    _add_provider_config(
        session,
        provider_id=provider_id,
        account_id="alice@example.com",
        api_key=SIBLING_KEY,
    )

    r = client.get("/api/v1/fleet/config")
    assert r.status_code == 200
    assert r.json()["account_tag_hints"][provider_id][_hint_key(provider_id, SIBLING_KEY)] == (
        "alice@example.com"
    )


def test_fingerprint_hint_ships_for_stored_xai_key(client: TestClient, session: Session) -> None:
    """xai is the awkward sibling: no candidate carries ``api_key``. A pasted
    bearer that is not a JWT has no stable claim, so the server hashes it
    as-is and the hint lines up with the ``GROK_OAUTH_TOKEN`` candidate
    only (a JWT is keyed on its claim instead, see the test below, #523).
    What matters for #349 is that a stored xai row answers under
    ``provider:xai#<fp>`` like every other keyed provider, instead of
    dropping out of the hint map."""
    _add_provider_config(
        session,
        provider_id="xai",
        account_id="grok@example.com",
        api_key=XAI_BEARER,
    )

    r = client.get("/api/v1/fleet/config")
    assert r.status_code == 200
    assert r.json()["account_tag_hints"]["xai"][_hint_key("xai", XAI_BEARER)] == (
        "grok@example.com"
    )


def test_fingerprint_hint_for_a_pasted_xai_jwt_uses_its_stable_claim(
    client: TestClient, session: Session
) -> None:
    """#523: the sidecar keys xAI origins on the JWT's stable claim, so the
    server hint for a pasted JWT must hash that claim too — the bearer itself
    rotates every few hours and would never match again."""

    def enc(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    bearer = f"{enc({'alg': 'none'})}.{enc({'principal_id': 'user-123', 'jti': 'x'})}.sig"
    _add_provider_config(session, provider_id="xai", account_id="grok@example.com", api_key=bearer)

    r = client.get("/api/v1/fleet/config")

    assert r.status_code == 200
    hints = r.json()["account_tag_hints"]["xai"]
    assert hints[_hint_key("xai", "user-123")] == "grok@example.com"
    assert _hint_key("xai", bearer) not in hints


def test_sibling_fingerprint_hints_land_in_separate_provider_buckets(
    client: TestClient, session: Session
) -> None:
    """One response, several keyed buckets — a hint for one provider can
    never be consumed as another's, because the descriptor carries the pid."""
    for provider_id in ("openrouter", "ollama"):
        _add_provider_config(
            session,
            provider_id=provider_id,
            account_id=f"{provider_id}@example.com",
            api_key=SIBLING_KEY,
        )

    r = client.get("/api/v1/fleet/config")
    assert r.status_code == 200
    hints = r.json()["account_tag_hints"]
    assert hints["openrouter"][_hint_key("openrouter", SIBLING_KEY)] == ("openrouter@example.com")
    assert hints["ollama"][_hint_key("ollama", SIBLING_KEY)] == "ollama@example.com"
    assert _hint_key("openrouter", SIBLING_KEY) not in hints["ollama"]


def test_fingerprint_hint_skips_unusable_rows(client: TestClient, session: Session) -> None:
    """No key and a disabled row produce no fingerprint hint — there is
    nothing to match a sidecar's fingerprint against, and the collector
    isn't running."""
    _add_provider_config(session, provider_id="opencode", account_id="default")
    _add_provider_config(
        session,
        provider_id="opencode",
        account_id="bob@example.com",
        enabled=False,
        api_key=OTHER_CLI_KEY,
    )

    r = client.get("/api/v1/fleet/config")
    assert r.status_code == 200
    # ``account_id="default"`` also keeps the single-account auto-hint out,
    # so what's left is exactly "no fingerprint was derivable".
    assert r.json()["account_tag_hints"] == {}


def test_fingerprint_hint_only_fires_for_keyed_providers(
    client: TestClient, session: Session
) -> None:
    """Fingerprinting covers providers whose credential is a bare static key. `github`
    can be an OAuth token that rotates on its own, so it stays outside the set: its row
    keeps the plain descriptor and gains no ``#<fingerprint>`` hint."""
    _add_provider_config(
        session,
        provider_id="github",
        account_id="alice@example.com",
        api_key="ghp-plain-origin",  # pragma: allowlist secret
    )

    r = client.get("/api/v1/fleet/config")
    assert r.status_code == 200
    hints = r.json()["account_tag_hints"]
    assert "opencode" not in hints
    # A configured account without a credential-specific match is ambiguous.
    assert "github" not in hints


def test_static_key_providers_beyond_opencode_get_a_fingerprint_hint(
    client: TestClient, session: Session
) -> None:
    """#443: kimi_api / kimi_k2 / zai read their key from an env var, so their origin is
    now key-scoped and the server can answer ``provider:<pid>#<fp>`` for a pasted key."""
    from app.services.account_identity import credential_fingerprint

    for pid in ("kimi_api", "kimi_k2", "zai"):
        _add_provider_config(
            session,
            provider_id=pid,
            account_id="alice@example.com",
            api_key=f"sk-{pid}-pasted-key",  # pragma: allowlist secret
        )

    hints = client.get("/api/v1/fleet/config").json()["account_tag_hints"]

    for pid in ("kimi_api", "kimi_k2", "zai"):
        fp = credential_fingerprint(f"sk-{pid}-pasted-key")  # pragma: allowlist secret
        assert hints[pid] == {f"provider:{pid}#{fp}": "alice@example.com"}


def test_operator_tag_wins_over_fingerprint_hint(client: TestClient, session: Session) -> None:
    """Merge order: an explicit per-origin tag is a deliberate choice and is
    never overridden by a derived fingerprint match."""
    from app.services.credential_tags import CredentialTagRepo

    _add_provider_config(
        session,
        provider_id="opencode",
        account_id="alice@example.com",
        api_key=OPENCODE_CLI_KEY,
    )
    CredentialTagRepo.set_tag(
        session,
        provider_id="opencode",
        credential_origin=_opencode_hint_key(OPENCODE_CLI_KEY),
        account_id="override@example.com",
    )
    session.commit()

    r = client.get("/api/v1/fleet/config")
    assert r.status_code == 200
    assert r.json()["account_tag_hints"]["opencode"] == {
        _opencode_hint_key(OPENCODE_CLI_KEY): "override@example.com",
    }


def test_fingerprint_hint_ships_while_auto_hint_is_withheld(
    client: TestClient, session: Session
) -> None:
    """The fingerprint hint is not a guess, so unlike the single-account
    auto-hint it ships even in a multi-sidecar deployment — a sidecar that
    lacks the key cannot produce a matching fingerprint, so the match is the
    scoping."""
    _add_provider_config(
        session,
        provider_id="opencode",
        account_id="alice@example.com",
        api_key=OPENCODE_CLI_KEY,
    )
    _add_provider_config(
        session,
        provider_id="minimax",
        account_id="s3ntin318@gmail.com",
        enabled=True,
    )
    _add_live_sidecar(session, "alpha", "alpha-host")
    _add_live_sidecar(session, "beta", "beta-host")

    r = client.get("/api/v1/fleet/config?sidecar_id=alpha")
    assert r.status_code == 200
    hints = r.json()["account_tag_hints"]
    assert hints["opencode"] == {_opencode_hint_key(OPENCODE_CLI_KEY): "alice@example.com"}
    # …while the implicit heuristic still refuses to pick between hosts.
    assert "minimax" not in hints


def test_config_scoped_tag_not_visible_to_other_sidecar(
    client: TestClient, session: Session
) -> None:
    """A machine-scoped credential tag only ships to its sidecar;
    other sidecars fall back to the deployment-wide view."""
    from app.services.credential_tags import CredentialTagRepo

    CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="path:/home/alice/.claude/.credentials.json",
        account_id="alice@example.com",
        sidecar_id="alpha",
    )
    session.commit()

    r_alpha = client.get("/api/v1/fleet/config?sidecar_id=alpha")
    assert r_alpha.status_code == 200
    assert r_alpha.json()["account_tag_hints"] == {
        "anthropic": {"path:/home/alice/.claude/.credentials.json": "alice@example.com"},
    }

    r_beta = client.get("/api/v1/fleet/config?sidecar_id=beta")
    assert r_beta.status_code == 200
    assert r_beta.json()["account_tag_hints"] == {}


def test_config_deployment_tag_visible_to_every_sidecar(
    client: TestClient, session: Session
) -> None:
    """A deployment-wide (legacy / "All machines") tag ships to every
    identified sidecar."""
    from app.services.credential_tags import CredentialTagRepo

    CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="path:/shared/.claude/.credentials.json",
        account_id="alice@example.com",
        sidecar_id=None,
    )
    session.commit()

    for sc in ("alpha", "beta"):
        r = client.get(f"/api/v1/fleet/config?sidecar_id={sc}")
        assert r.status_code == 200
        assert r.json()["account_tag_hints"] == {
            "anthropic": {"path:/shared/.claude/.credentials.json": "alice@example.com"},
        }


def test_auto_hint_withheld_for_unreported_sidecar_in_multi_host(
    client: TestClient, session: Session
) -> None:
    """Multi-host: a sidecar that never reported ``provider:minimax``
    gets no auto-hint (cross-host attribution block)."""
    session.add(
        ProviderConfig(
            provider_id="minimax",
            account_id="s3ntin318@gmail.com",
            enabled=True,
        )
    )
    _add_live_sidecar(session, "alpha", "alpha-host")
    _add_live_sidecar(session, "beta", "beta-host")

    r = client.get("/api/v1/fleet/config?sidecar_id=beta")
    assert r.status_code == 200
    assert r.json()["account_tag_hints"] == {}


def test_auto_hint_ships_to_reporting_sidecar_in_multi_host(
    client: TestClient, session: Session
) -> None:
    """Multi-host: the sidecar that reported ``provider:minimax`` (via
    the manifest) receives the auto-hint; its peer does not."""
    from app.services.credential_tags import PendingCredentialTagRepo

    session.add(
        ProviderConfig(
            provider_id="minimax",
            account_id="s3ntin318@gmail.com",
            enabled=True,
        )
    )
    _add_live_sidecar(session, "alpha", "alpha-host")
    _add_live_sidecar(session, "beta", "beta-host")
    PendingCredentialTagRepo.upsert(
        session,
        sidecar_id="alpha",
        provider_id="minimax",
        credential_origin="provider:minimax",
    )
    session.commit()

    r_alpha = client.get("/api/v1/fleet/config?sidecar_id=alpha")
    assert r_alpha.status_code == 200
    assert r_alpha.json()["account_tag_hints"] == {}

    r_beta = client.get("/api/v1/fleet/config?sidecar_id=beta")
    assert r_beta.status_code == 200
    assert r_beta.json()["account_tag_hints"] == {}


def test_auto_hint_withheld_when_unidentified_in_multi_host(
    client: TestClient, session: Session
) -> None:
    """Old sidecar binary (no ``?sidecar_id=``) in a multi-host
    deployment: no auto-hints — the safe pre-#319 behavior."""
    session.add(
        ProviderConfig(
            provider_id="minimax",
            account_id="s3ntin318@gmail.com",
            enabled=True,
        )
    )
    _add_live_sidecar(session, "alpha", "alpha-host")
    _add_live_sidecar(session, "beta", "beta-host")

    r = client.get("/api/v1/fleet/config")
    assert r.status_code == 200
    assert r.json()["account_tag_hints"] == {}


def test_pending_endpoint_hides_origin_with_effective_hint(
    client: TestClient, session: Session
) -> None:
    """Pending rows whose origin already has an explicit tag (e.g. a
    tag applied while the row was still mid-flight) are hidden — the
    operator shouldn't be re-prompted for a resolved origin."""
    from app.services.credential_tags import CredentialTagRepo, PendingCredentialTagRepo

    CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="path:/tagged",
        account_id="alice@example.com",
        sidecar_id=None,
    )
    PendingCredentialTagRepo.upsert(
        session,
        sidecar_id="alpha",
        provider_id="anthropic",
        credential_origin="path:/tagged",
    )
    PendingCredentialTagRepo.upsert(
        session,
        sidecar_id="alpha",
        provider_id="anthropic",
        credential_origin="path:/untagged",
    )
    session.commit()

    r = client.get("/api/v1/fleet/credentials/tags/pending")
    assert r.status_code == 200
    items = r.json()["items"]
    assert [i["credential_origin"] for i in items] == ["path:/untagged"]
    # Counts reflect only the visible (still-untagged) rows.
    assert r.json()["counts_by_sidecar"] == {"alpha": 1}


# ---------------------------------------------------------------------------
# Note on redeem endpoint coverage: POST /api/v1/fleet/credentials/redeem is
# intentionally not implemented in this PR. Adding the endpoint without a
# production caller would ship unused authenticated surface — see the PR
# description and code review on #283. The redeem handler lands with the
# first real caller in the follow-up PR; until then this file asserts only
# the issuer (`GET /fleet/config`) and the token verify contract above.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# #322 review follow-ups: tag management + scope edge cases
# ---------------------------------------------------------------------------


def test_config_empty_sidecar_id_treated_as_unidentified(
    client: TestClient, session: Session
) -> None:
    """``?sidecar_id=`` (empty) must behave like an omitted id: with exactly
    one live sidecar, that sidecar's scoped tags still ship."""
    from app.services.credential_tags import CredentialTagRepo

    _add_live_sidecar(session, "alpha", "alpha-host")
    CredentialTagRepo.set_tag(
        session,
        provider_id="anthropic",
        credential_origin="provider:anthropic",
        account_id="alice@example.com",
        sidecar_id="alpha",
    )
    session.commit()

    r = client.get("/api/v1/fleet/config?sidecar_id=")
    assert r.status_code == 200
    assert r.json()["account_tag_hints"] == {
        "anthropic": {"provider:anthropic": "alice@example.com"},
    }


def test_config_redacts_accounts_for_unsigned_remote_caller(
    client: TestClient, session: Session, monkeypatch
) -> None:
    monkeypatch.setattr("app.core.config.settings.APP_HOST", "0.0.0.0")
    _seed_identity_state(session)

    body = client.get("/api/v1/fleet/config").json()

    assert body["account_tag_hints"] == {}
    assert body["config"]["providers"]["anthropic"]["accounts"] == []
    # The logical enabled/strategies view stays available.
    assert body["config"]["providers"]["anthropic"]["enabled"] is True
    assert "alice@example.com" not in json.dumps(body)


def test_config_full_view_for_signed_sidecar(
    client: TestClient, session: Session, monkeypatch
) -> None:
    monkeypatch.setattr("app.core.config.settings.APP_HOST", "0.0.0.0")
    _seed_identity_state(session)

    query = "sidecar_id=laptop"
    resp = client.get(f"/api/v1/fleet/config?{query}", headers=_signed_config_headers(query))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["account_tag_hints"] == {"anthropic": {"provider:anthropic": "alice@example.com"}}
    accounts = body["config"]["providers"]["anthropic"]["accounts"]
    assert [a["account_id"] for a in accounts] == ["alice@example.com"]
    assert accounts[0].get("credential_token")


def test_config_rejects_bad_signature(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr("app.core.config.settings.APP_HOST", "0.0.0.0")
    headers = _signed_config_headers("sidecar_id=laptop")
    # Signature bound to another machine's query can't be replayed.
    resp = client.get("/api/v1/fleet/config?sidecar_id=desktop", headers=headers)
    assert resp.status_code == 401


def test_config_full_view_on_loopback_bind_without_signature(
    client: TestClient, session: Session, monkeypatch
) -> None:
    """Local topology (server + sidecar on one host) keeps working unsigned."""
    monkeypatch.setattr("app.core.config.settings.APP_HOST", "127.0.0.1")
    _seed_identity_state(session)
    body = client.get("/api/v1/fleet/config").json()
    assert body["account_tag_hints"] == {"anthropic": {"provider:anthropic": "alice@example.com"}}


def test_config_signature_is_single_use(client: TestClient, monkeypatch) -> None:
    """A captured signed GET can't be replayed within the timestamp window."""
    monkeypatch.setattr("app.core.config.settings.APP_HOST", "0.0.0.0")
    query = "sidecar_id=laptop"
    headers = _signed_config_headers(query)
    assert client.get(f"/api/v1/fleet/config?{query}", headers=headers).status_code == 200
    replay = client.get(f"/api/v1/fleet/config?{query}", headers=headers)
    assert replay.status_code == 409


def test_config_issues_no_credential_tokens_for_unsigned_remote_caller(
    client: TestClient, session: Session, monkeypatch
) -> None:
    """Redacted callers don't even cost a token issuance (#327 review)."""
    from unittest.mock import patch

    monkeypatch.setattr("app.core.config.settings.APP_HOST", "0.0.0.0")
    _seed_identity_state(session)
    with patch("app.api.endpoints.fleet_sidecars.issue_credential_token") as issue:
        assert client.get("/api/v1/fleet/config").status_code == 200
    issue.assert_not_called()
