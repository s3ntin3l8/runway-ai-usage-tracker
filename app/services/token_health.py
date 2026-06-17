"""Token health inspection — expiry parsing, status classification."""

import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session
from sqlmodel import select as sqlselect

from app.core.config import settings
from app.core.db import engine
from app.core.utils import IdentityExtractor, scrub_log
from app.models.db import ProviderConfig, SidecarRegistry
from app.services.token_cache import token_cache

logger = logging.getLogger(__name__)

EXPIRY_WARNING_SECS = 86400  # 24 hours — for tokens that require manual re-auth


def _classify_status(
    exp: float | None,
    is_opaque: bool = False,
    can_refresh: bool = False,
) -> str:
    if exp is None:
        # If we have no JWT expiry, but we have a token value (is_opaque),
        # it's considered "valid" (READY).
        return "valid" if is_opaque else "unknown"
    now = time.time()
    if exp < now:
        return "expired"
    seconds_left = exp - now

    # Tokens with a refresh_token are auto-rolled by TokenAutoRefresher every
    # TOKEN_AUTO_REFRESH_INTERVAL_SECONDS. Short-lived access tokens (Gemini's
    # 60-min JWT) would otherwise sit permanently in the 24h "expiring" bucket.
    # Only warn when the next auto-refresh tick is too late to save the token.
    if can_refresh and settings.TOKEN_AUTO_REFRESH_ENABLED:
        if seconds_left < settings.TOKEN_AUTO_REFRESH_INTERVAL_SECONDS:
            return "expiring"
        return "valid"

    if seconds_left < EXPIRY_WARNING_SECS:
        return "expiring"
    return "valid"


class TokenHealthService:
    async def get_health(self) -> list[dict[str, Any]]:
        """Return a health record for each cached credential."""
        stats = await token_cache.get_all_stats()
        result = []
        seen_token_values = set()

        sidecar_names = {}
        try:
            with Session(engine) as _s:
                for sc in _s.exec(sqlselect(SidecarRegistry)).all():
                    sidecar_names[sc.sidecar_id] = sc.custom_name or sc.hostname or sc.sidecar_id
        except Exception as e:
            logger.warning(f"Could not load sidecar names for token health: {e}")

        for provider, accounts in stats.items():
            for acc_id, info in accounts.items():
                tokens = await token_cache.get(provider, acc_id) or {}
                logger.debug(f"Token health check for {provider}/{acc_id}: {list(tokens.keys())}")

                # Metadata lookup: Fallback to history if cache is missing account_label
                label = info.get("account_label")
                # Phase 1 schema reset: UsageSnapshot removed; label fallback is a no-op.
                # Will be replaced with LatestUsage / UsageEvent lookup in a later phase.

                exp = IdentityExtractor.exp_from_tokens(tokens)

                # If we have any tokens, track their hashes to deduplicate later
                for val in tokens.values():
                    if val:
                        seen_token_values.add(f"{provider}:{val}")

                # If no JWT expiry found, but we have ANY token, it's opaque/ready
                is_opaque = (exp is None) and (len(tokens) > 0)
                can_refresh = "refresh_token" in tokens

                source_val = info.get("source")
                source_name = sidecar_names.get(source_val, source_val) if source_val else None

                result.append(
                    {
                        "provider": provider,
                        "account_id": acc_id,
                        "account_label": label,
                        "source": source_val,
                        "source_name": source_name,
                        "token_types": list(tokens.keys()),
                        "status": _classify_status(
                            exp, is_opaque=is_opaque, can_refresh=can_refresh
                        ),
                        "expires_at": (
                            datetime.fromtimestamp(exp, tz=UTC).isoformat()
                            if exp is not None
                            else None
                        ),
                        "ttl_remaining_seconds": info.get("ttl_remaining", 0),
                        "can_refresh": can_refresh,
                    }
                )

        # Also surface API keys / session cookies configured in Settings → Providers.
        # These are stored encrypted in ProviderConfig but never flow through token_cache,
        # so they would otherwise be invisible to the Token Health panel.
        try:
            with Session(engine) as _s:
                configs = _s.exec(
                    sqlselect(ProviderConfig).where(ProviderConfig.enabled == True)  # noqa: E712
                ).all()

            for cfg in configs:
                # 1. API Keys
                if cfg.api_key:
                    # Skip if this exact token is already in the live session cache
                    if f"{cfg.provider_id}:{cfg.api_key}" in seen_token_values:
                        continue

                    # Fallback label lookup
                    label = cfg.account_label
                    # Phase 1 schema reset: UsageSnapshot removed; label fallback is a no-op.

                    result.append(
                        {
                            "provider": cfg.provider_id,
                            "account_id": "config",
                            "account_label": label,
                            "source": "config",
                            "source_name": "config",
                            "token_types": ["api_key"],
                            "status": "valid",  # Static keys are assumed ready
                            "expires_at": None,
                            "ttl_remaining_seconds": 0,
                            "can_refresh": False,
                        }
                    )

                # 2. Session Cookies
                if cfg.session_cookie:
                    # Skip if already in cache
                    if f"{cfg.provider_id}:{cfg.session_cookie}" in seen_token_values:
                        continue

                    label = cfg.account_label
                    # Phase 1 schema reset: UsageSnapshot removed; label fallback is a no-op.

                    result.append(
                        {
                            "provider": cfg.provider_id,
                            "account_id": "config-cookie",
                            "account_label": label,
                            "source": "config",
                            "source_name": "config",
                            "token_types": ["session_cookie"],
                            "status": "valid",
                            "expires_at": None,
                            "ttl_remaining_seconds": 0,
                            "can_refresh": False,
                        }
                    )
        except Exception as e:
            logger.warning(f"Could not load ProviderConfig credentials for token health: {e}")

        # 3. Local File Discovery (GitHub OAuth)
        try:
            import os

            from app.core.config import settings

            if os.path.exists(settings.GITHUB_OAUTH_PATH):
                # Try to find a custom label for GitHub in the DB configs first
                label = None
                for cfg in configs:
                    if cfg.provider_id == "github" and cfg.account_label:
                        label = cfg.account_label
                        break

                with open(settings.GITHUB_OAUTH_PATH) as f:
                    data = json.load(f)
                    token = data.get("access_token")

                    if not label:
                        user = data.get("user") or {}
                        label = user.get("email") or user.get("login")

                    # Last fallback: Phase 1 schema reset: UsageSnapshot removed; use static default.
                    if not label:
                        label = "GitHub"

                    if token and f"github:{token}" not in seen_token_values:
                        result.append(
                            {
                                "provider": "github",
                                "account_id": "local-file",
                                "account_label": label,
                                "source": "config",
                                "source_name": "config",
                                "token_types": ["oauth_token"],
                                "status": "valid",
                                "expires_at": None,
                                "ttl_remaining_seconds": 0,
                                "can_refresh": False,
                            }
                        )
        except Exception as e:
            logger.debug(f"GitHub file health scan failed: {e}")

        # Mark expired, unrefreshable entries as "redundant" when another
        # credential for the same provider is still healthy. Such an entry can't
        # be auto-rolled (no refresh_token) and isn't blocking collection — so
        # the dashboard banner should not raise a hard alarm on it alone. When
        # *every* credential for a provider is dead, none are redundant and the
        # alarm still fires.
        healthy_providers = {r["provider"] for r in result if r["status"] in ("valid", "expiring")}
        for r in result:
            r["redundant"] = (
                r["status"] == "expired"
                and not r["can_refresh"]
                and r["provider"] in healthy_providers
            )

        return result

    async def delete_credential(self, provider: str, account_id: str) -> bool:
        """
        Manually remove a credential from the in-memory cache OR database.
        Returns true if removed.
        """
        # If it's a static config entry, delete it from the database
        if account_id in ("config", "config-cookie"):
            try:
                with Session(engine) as session:
                    stmt = sqlselect(ProviderConfig).where(
                        ProviderConfig.provider_id == provider,
                        ProviderConfig.enabled == True,  # noqa: E712
                    )
                    cfg = session.exec(stmt).first()
                    if cfg:
                        if account_id == "config":
                            cfg.api_key_encrypted = None
                        else:
                            cfg.session_cookie_encrypted = None

                        # If both are now empty, we could delete the row, but let's just clear the fields
                        session.add(cfg)
                        session.commit()
                        logger.info(f"Cleared DB credential for {scrub_log(provider)}")
                        return True
            except Exception as e:
                logger.error(f"Failed to delete DB credential: {e}")
                return False

        # Otherwise, purge from memory cache
        return await token_cache.remove(provider, account_id)


token_health_service = TokenHealthService()
