"""Static-key env origins are key-scoped (#443) and the tags written before that still apply."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlmodel import SQLModel, create_engine, select
from sqlmodel.orm.session import Session
from sqlmodel.pool import StaticPool

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.models.db import CredentialSource, CredentialTag  # noqa: E402
from app.services.account_identity import keyed_credential_origin  # noqa: E402
from app.services.credential_sources import (  # noqa: E402
    is_machine_bound_origin,
    retire_superseded_keyed_origins,
    retire_unkeyed_origin,
    sidecar_source_id,
)
from app.services.credential_tags import CredentialTagRepo, origin_candidates  # noqa: E402
from scripts import sidecar  # noqa: E402
from scripts.sidecar_pkg.identity import credential_fingerprint  # noqa: E402

KEY = "static-key-value-123"  # pragma: allowlist secret


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


# --- the sidecar ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "variable"),
    [("kimi_api", "KIMI_API_KEY"), ("zai", "ZAI_API_KEY"), ("kimi_k2", "KIMI_K2_API_KEY")],
)
def test_env_key_origins_carry_the_credential_fingerprint(monkeypatch, provider, variable):
    rule = next(
        r
        for r in sidecar.__REGISTRY__["providers"][provider]["rules"]
        if r["type"] == "env" and r["mapping"].get("value") == "api_key"
    )
    monkeypatch.setenv(rule["variable"], KEY)
    config = {"name": provider, "icon": "k", "rules": [rule]}

    _cards, blocked = sidecar.GenericCollector.collect_provider(provider, config)

    fp = credential_fingerprint(KEY)
    assert blocked == [
        {
            "provider_id": provider,
            "credential_origin": f"env:{rule['variable']}#{fp}",
            "reason": "token_withheld",
        }
    ]


def test_a_rotated_key_is_a_new_origin_so_an_old_tag_cannot_follow_it(monkeypatch):
    rule = {"type": "env", "variable": "ZAI_API_KEY", "mapping": {"value": "api_key"}}
    config = {"name": "zai", "icon": "k", "rules": [rule]}
    origins = []
    for key in ("old-key-0000000001", "new-key-0000000002"):
        monkeypatch.setenv("ZAI_API_KEY", key)
        _c, blocked = sidecar.GenericCollector.collect_provider("zai", config)
        origins.append(blocked[0]["credential_origin"])

    assert origins[0] != origins[1]


@pytest.mark.parametrize("provider", ["anthropic", "gemini", "chatgpt", "antigravity", "github"])
def test_rotating_oauth_providers_stay_unfingerprinted(provider):
    from scripts.sidecar_pkg.identity import FINGERPRINTED_ORIGIN_PROVIDERS

    assert provider not in FINGERPRINTED_ORIGIN_PROVIDERS


def test_cookie_origins_stay_plain(monkeypatch):
    """A cookie rotates with every login; a fingerprint would orphan its tag each time."""
    from scripts.sidecar import fingerprinted_credential_origin

    assert (
        fingerprinted_credential_origin(
            "cookie:kimi_coding/session", "kimi_coding", {"session_cookie": "c"}
        )
        == "cookie:kimi_coding/session"
    )


# --- tags written before the origin was key-scoped ----------------------------------------


def test_origin_candidates_put_the_exact_origin_first_then_the_plain_one():
    keyed = keyed_credential_origin("env:ZAI_API_KEY", "0123456789ab")
    assert origin_candidates(keyed) == [keyed, "env:ZAI_API_KEY"]
    assert origin_candidates("env:ZAI_API_KEY") == ["env:ZAI_API_KEY"]
    assert origin_candidates("path:/a#not-a-fingerprint") == ["path:/a#not-a-fingerprint"]


def _tag(session, origin, account, sidecar_id=None):
    CredentialTagRepo.set_tag(
        session,
        provider_id="zai",
        credential_origin=origin,
        account_id=account,
        sidecar_id=sidecar_id,
        set_by="operator",
    )
    session.commit()


def test_a_plain_origin_tag_still_applies_to_the_keyed_origin(session):
    keyed = keyed_credential_origin("env:ZAI_API_KEY", "0123456789ab")
    _tag(session, "env:ZAI_API_KEY", "legacy@example.com")

    assert (
        CredentialTagRepo.get_account_id(session, provider_id="zai", credential_origin=keyed)
        == "legacy@example.com"
    )
    tag = CredentialTagRepo.get(session, provider_id="zai", credential_origin=keyed)
    assert tag is not None and tag.account_id == "legacy@example.com"


def test_a_tag_on_the_exact_keyed_origin_beats_the_plain_one(session):
    keyed = keyed_credential_origin("env:ZAI_API_KEY", "0123456789ab")
    _tag(session, "env:ZAI_API_KEY", "legacy@example.com")
    _tag(session, keyed, "specific@example.com")

    assert (
        CredentialTagRepo.get_account_id(session, provider_id="zai", credential_origin=keyed)
        == "specific@example.com"
    )


def test_the_plain_fallback_respects_machine_scope(session):
    keyed = keyed_credential_origin("env:ZAI_API_KEY", "0123456789ab")
    _tag(session, "env:ZAI_API_KEY", "laptop-only@example.com", sidecar_id="laptop")

    assert (
        CredentialTagRepo.get_account_id(
            session, provider_id="zai", credential_origin=keyed, sidecar_id="laptop"
        )
        == "laptop-only@example.com"
    )
    assert (
        CredentialTagRepo.get_account_id(
            session, provider_id="zai", credential_origin=keyed, sidecar_id="desktop"
        )
        is None
    )


def test_the_inventory_resolves_a_legacy_tag_for_a_keyed_origin():
    from app.models.db import CredentialTag
    from app.services.credential_inventory import _resolve_tag

    keyed = keyed_credential_origin("env:ZAI_API_KEY", "0123456789ab")
    legacy = CredentialTag(
        provider_id="zai",
        credential_origin="env:ZAI_API_KEY",
        account_id="legacy@example.com",
        sidecar_id=None,
        set_by="operator",
    )
    row = CredentialSource(
        provider_id="zai",
        account_id="default",
        source_id="sidecar:x",
        source_type="env",
        source_label="ZAI_API_KEY",
        credential_origin=keyed,
        sidecar_id="laptop",
    )

    assert _resolve_tag({("zai", "env:ZAI_API_KEY"): [legacy]}, row) is legacy
    assert _resolve_tag({}, row) is None


# --- retiring the superseded row -------------------------------------------------------------


def _row(session, origin, sidecar_id="laptop", provider="zai"):
    session.add(
        CredentialSource(
            provider_id=provider,
            account_id="a@example.com",
            source_id=sidecar_source_id(sidecar_id, origin),
            source_type="env",
            source_label="x",
            credential_origin=origin,
            sidecar_id=sidecar_id,
        )
    )
    session.commit()


def test_retire_unkeyed_origin_deletes_only_the_superseded_row(session):
    keyed = keyed_credential_origin("env:ZAI_API_KEY", "0123456789ab")
    _row(session, "env:ZAI_API_KEY")
    _row(session, "env:ZAI_API_KEY", sidecar_id="desktop")
    _row(session, "env:ZAI_API_KEY", provider="kimi_api")

    retired = retire_unkeyed_origin(session, provider_id="zai", sidecar_id="laptop", origin=keyed)
    session.commit()

    assert [r.sidecar_id for r in retired] == ["laptop"]
    left = {(r.provider_id, r.sidecar_id) for r in session.exec(select(CredentialSource)).all()}
    assert left == {("zai", "desktop"), ("kimi_api", "laptop")}


def test_retire_unkeyed_origin_ignores_a_plain_origin(session):
    _row(session, "env:ZAI_API_KEY")

    assert (
        retire_unkeyed_origin(
            session, provider_id="zai", sidecar_id="laptop", origin="env:ZAI_API_KEY"
        )
        == []
    )


def _xai_tag(session, origin, set_by="rotation", sidecar_id="laptop", provider="xai"):
    session.add(
        CredentialTag(
            provider_id=provider,
            credential_origin=origin,
            account_id="a@example.com",
            sidecar_id=sidecar_id,
            set_by=set_by,
        )
    )
    session.commit()


def test_retire_superseded_keyed_origins_drops_old_fingerprints_of_a_rotating_provider(session):
    base = "path:/home/u/.local/share/opencode/auth.json"
    old1, old2 = f"{base}#aaaaaaaaaaaa", f"{base}#bbbbbbbbbbbb"
    new = f"{base}#cccccccccccc"
    for origin in (old1, old2, new):
        _row(session, origin, provider="xai")
        _xai_tag(session, origin)
    _row(session, old1, sidecar_id="desktop", provider="xai")  # another machine
    _row(session, f"{base}-other#dddddddddddd", provider="xai")  # a different file
    _xai_tag(session, f"{base}#eeeeeeeeeeee", set_by="operator")  # the operator's own tag

    retired = retire_superseded_keyed_origins(
        session, provider_id="xai", sidecar_id="laptop", origin=new
    )
    session.commit()

    assert {r.credential_origin for r in retired} == {old1, old2}
    left = {(r.sidecar_id, r.credential_origin) for r in session.exec(select(CredentialSource))}
    assert left == {
        ("laptop", new),
        ("desktop", old1),
        ("laptop", f"{base}-other#dddddddddddd"),
    }
    tags = {(t.set_by, t.credential_origin) for t in session.exec(select(CredentialTag))}
    assert tags == {("rotation", new), ("operator", f"{base}#eeeeeeeeeeee")}


def test_retire_superseded_keyed_origins_leaves_other_providers_alone(session):
    """A provider holding several distinct keys under one path would delete them
    from each other every cycle; only providers that re-key a single login opt in."""
    base = "path:/home/u/.local/share/opencode/auth.json"
    _row(session, f"{base}#aaaaaaaaaaaa", provider="zai")

    assert (
        retire_superseded_keyed_origins(
            session, provider_id="zai", sidecar_id="laptop", origin=f"{base}#bbbbbbbbbbbb"
        )
        == []
    )
    assert len(session.exec(select(CredentialSource)).all()) == 1


def test_retire_superseded_keyed_origins_ignores_a_plain_origin(session):
    _row(session, "path:/a/auth.json#aaaaaaaaaaaa", provider="xai")

    assert (
        retire_superseded_keyed_origins(
            session, provider_id="xai", sidecar_id="laptop", origin="path:/a/auth.json"
        )
        == []
    )


# --- machine-bound origins ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("origin", "bound"),
    [
        ("cookie:kimi_coding/session", True),
        ("keychain:Claude Code-credentials", True),
        ("path:/shared/auth.json", False),
        ("env:ZAI_API_KEY#0123456789ab", False),
        ("provider:xai", False),
        (None, False),
        ("", False),
    ],
)
def test_machine_bound_origins(origin, bound):
    assert is_machine_bound_origin(origin) is bound
