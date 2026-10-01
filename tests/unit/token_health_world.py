"""A realistic mixed credential "world" on a real in-memory database.

Used to characterize ``TokenHealthService.get_health`` (and, through it, credential
alerts) so a refactor of how credential status is computed can be proven not to change
what Token Health emits. The existing token-health tests drive a MagicMock Session with
ordered ``.all()`` side effects, which pins query order rather than behaviour.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.models.db import CredentialSource, ProviderConfig, SidecarRegistry
from app.services import auth_failures
from app.services.token_cache import TokenCache

ALICE = "alice@example.com"
BOB = "bob@example.com"


def _ms(seconds_from_now: float) -> str:
    return str(int((time.time() + seconds_from_now) * 1000))


async def build_world(monkeypatch) -> None:
    """Seed the DB, the token cache, the server scan and the rejection flags."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    cache = TokenCache()
    monkeypatch.setattr("app.services.token_health.engine", engine)
    monkeypatch.setattr("app.services.token_health.token_cache", cache)
    auth_failures.reset()

    now = datetime.now(UTC)
    with Session(engine) as s:
        for sid, name in (("dev-01", "DEV-01"), ("macbook", "MacBook"), ("gone", "Gone")):
            s.add(SidecarRegistry(sidecar_id=sid, hostname=sid, custom_name=name))

        def source(provider, account, source_id, sidecar, **kw):
            s.add(
                CredentialSource(
                    provider_id=provider,
                    account_id=account,
                    source_id=source_id,
                    source_type="file",
                    source_label="creds.json",
                    credential_origin=f"path:/{source_id}",
                    sidecar_id=sidecar,
                    **kw,
                )
            )

        # Gemini: the same account on two machines (live bundles) and one that went away.
        source("gemini", ALICE, "sidecar:g1", "dev-01")
        source("gemini", ALICE, "sidecar:g2", "macbook")
        source(
            "gemini",
            ALICE,
            "sidecar:g3",
            "gone",
            last_seen=now - timedelta(days=3),
            token_types_json='["oauth_token"]',
            credential_expires_at=now - timedelta(days=1),
        )
        # An expired, non-rollable key beside a healthy one on the same account.
        source("openrouter", BOB, "sidecar:o1", "dev-01")
        # A pasted key and a pasted cookie in Settings → Providers. (SQLModel ignores
        # property names passed as constructor kwargs, so set them through the setters.)
        for provider, attr, value in (
            ("openrouter", "api_key", "sk-cfg-key"),  # pragma: allowlist secret
            ("ollama", "session_cookie", "cfg-cookie"),  # pragma: allowlist secret
        ):
            cfg = ProviderConfig(provider_id=provider, account_id="default")
            setattr(cfg, attr, value)
            s.add(cfg)
        s.commit()

    # Live bundles for the two reporting machines; an aggregate-only Codex entry (no
    # source id, as the collectors' own refresh stores it); a pending Claude bundle that
    # exists only in the cache.
    for sid in ("g1", "g2"):
        await cache.store(
            "gemini",
            {"oauth_token": f"tok-{sid}", "refresh_token": f"rt-{sid}", "expiry_date": _ms(7200)},
            account_id=ALICE,
            source_id=f"sidecar:{sid}",
            source="dev-01" if sid == "g1" else "macbook",
            source_metadata={"sidecar_id": "dev-01" if sid == "g1" else "macbook"},
        )
    sidecar_key = {"api_key": "sk-sidecar"}  # pragma: allowlist secret
    await cache.store(
        "openrouter",
        sidecar_key,
        account_id=BOB,
        source_id="sidecar:o1",
        source="dev-01",
        source_metadata={"sidecar_id": "dev-01"},
    )
    await cache.store(
        "chatgpt",
        {"oauth_token": "codex-tok", "refresh_token": "codex-rt", "expiry_date": _ms(-60)},
        account_id=ALICE,
        source="macbook",
    )
    await cache.store(
        "anthropic",
        {"oauth_token": "claude-pending", "refresh_token": "claude-rt"},
        account_id="sidecar:pending",
        source_id="sidecar:pending",
        source="dev-01",
        source_metadata={"sidecar_id": "dev-01", "identity_pending": True},
    )

    monkeypatch.setattr(
        "app.services.token_health._collect_server_credentials",
        lambda: {"github": {"api_key": "ghp_env"}},  # pragma: allowlist secret
    )
    # The provider rejected Bob's key.
    auth_failures.mark("openrouter", BOB)
