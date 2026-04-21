"""Token health inspection — expiry parsing, status classification."""

import base64
import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session, desc
from sqlmodel import select as sqlselect

from app.core.db import engine
from app.models.db import ProviderConfig, UsageSnapshot
from app.services.token_cache import token_cache

logger = logging.getLogger(__name__)

EXPIRY_WARNING_SECS = 86400  # 24 hours


def _parse_jwt_exp(token: str) -> float | None:
    """Extract the 'exp' claim from a JWT without verifying the signature."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        padding = "=" * (4 - len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + padding))
        exp = payload.get("exp")
        return float(exp) if exp is not None else None
    except Exception:
        return None


def _classify_status(exp: float | None, is_opaque: bool = False) -> str:
    if exp is None:
        # If we have no JWT expiry, but we have a token value (is_opaque),
        # it's considered "valid" (READY).
        return "valid" if is_opaque else "unknown"
    now = time.time()
    if exp < now:
        return "expired"
    if exp - now < EXPIRY_WARNING_SECS:
        return "expiring"
    return "valid"


class TokenHealthService:
    async def get_health(self) -> list[dict[str, Any]]:
        """Return a health record for each cached credential."""
        stats = await token_cache.get_all_stats()
        result = []
        seen_token_values = set()

        for provider, accounts in stats.items():
            for acc_id, info in accounts.items():
                tokens = await token_cache.get(provider, acc_id) or {}
                logger.debug(f"Token health check for {provider}/{acc_id}: {list(tokens.keys())}")

                # Metadata lookup: Fallback to history if cache is missing account_label
                label = info.get("account_label")
                if not label:
                    # Query the most recent usage snapshot for this account to find a label (email)
                    with Session(engine) as session:
                        stmt = (
                            sqlselect(UsageSnapshot.account_label)
                            .where(
                                UsageSnapshot.provider_id == provider,
                                UsageSnapshot.account_id == acc_id,
                                UsageSnapshot.account_label != None,  # noqa: E711
                            )
                            .order_by(desc(UsageSnapshot.timestamp))
                            .limit(1)
                        )
                        label = session.exec(stmt).first()

                exp: float | None = None
                for key in ("oauth_token", "access_token", "id_token"):
                    if key in tokens:
                        exp = _parse_jwt_exp(tokens[key])
                        if exp is not None:
                            break

                # If we have any tokens, track their hashes to deduplicate later
                for val in tokens.values():
                    if val:
                        seen_token_values.add(f"{provider}:{val}")

                # If no JWT expiry found, but we have ANY token, it's opaque/ready
                is_opaque = (exp is None) and (len(tokens) > 0)

                result.append(
                    {
                        "provider": provider,
                        "account_id": acc_id,
                        "account_label": label,
                        "source": info.get("source"),
                        "token_types": list(tokens.keys()),
                        "status": _classify_status(exp, is_opaque=is_opaque),
                        "expires_at": (
                            datetime.fromtimestamp(exp, tz=UTC).isoformat()
                            if exp is not None
                            else None
                        ),
                        "ttl_remaining_seconds": info.get("ttl_remaining", 0),
                        "can_refresh": "refresh_token" in tokens,
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
                    if not label:
                        with Session(engine) as session:
                            stmt = (
                                sqlselect(UsageSnapshot.account_label)
                                .where(
                                    UsageSnapshot.provider_id == cfg.provider_id,
                                    UsageSnapshot.account_label != None,  # noqa: E711
                                )
                                .order_by(desc(UsageSnapshot.timestamp))
                                .limit(1)
                            )
                            label = session.exec(stmt).first()

                    result.append(
                        {
                            "provider": cfg.provider_id,
                            "account_id": "config",
                            "account_label": label,
                            "source": "config",
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
                    if not label:
                        with Session(engine) as session:
                            stmt = (
                                sqlselect(UsageSnapshot.account_label)
                                .where(
                                    UsageSnapshot.provider_id == cfg.provider_id,
                                    UsageSnapshot.account_label != None,  # noqa: E711
                                )
                                .order_by(desc(UsageSnapshot.timestamp))
                                .limit(1)
                            )
                            label = session.exec(stmt).first()

                    result.append(
                        {
                            "provider": cfg.provider_id,
                            "account_id": "config-cookie",
                            "account_label": label,
                            "source": "config",
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

                    # Last fallback: database history
                    if not label:
                        with Session(engine) as session:
                            stmt = (
                                sqlselect(UsageSnapshot.account_label)
                                .where(
                                    UsageSnapshot.provider_id == "github",
                                    UsageSnapshot.account_label != None,  # noqa: E711
                                )
                                .order_by(desc(UsageSnapshot.timestamp))
                                .limit(1)
                            )
                            label = session.exec(stmt).first() or "GitHub"

                    if token and f"github:{token}" not in seen_token_values:
                        result.append(
                            {
                                "provider": "github",
                                "account_id": "local-file",
                                "account_label": label,
                                "source": "config",
                                "token_types": ["oauth_token"],
                                "status": "valid",
                                "expires_at": None,
                                "ttl_remaining_seconds": 0,
                                "can_refresh": False,
                            }
                        )
        except Exception as e:
            logger.debug(f"GitHub file health scan failed: {e}")

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
                        logger.info(f"Cleared DB credential for {provider}")
                        return True
            except Exception as e:
                logger.error(f"Failed to delete DB credential: {e}")
                return False

        # Otherwise, purge from memory cache
        return await token_cache.remove(provider, account_id)


token_health_service = TokenHealthService()
