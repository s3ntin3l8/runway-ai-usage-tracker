"""Token health inspection — expiry parsing, status classification."""

import base64
import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

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


def _classify_status(exp: float | None) -> str:
    if exp is None:
        return "unknown"
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
        for provider, accounts in stats.items():
            for acc_id, info in accounts.items():
                tokens = await token_cache.get(provider, acc_id) or {}
                exp: float | None = None
                for key in ("oauth_token", "access_token", "id_token"):
                    if key in tokens:
                        exp = _parse_jwt_exp(tokens[key])
                        if exp is not None:
                            break
                result.append(
                    {
                        "provider": provider,
                        "account_id": acc_id,
                        "account_label": info.get("account_label"),
                        "token_types": list(tokens.keys()),
                        "status": _classify_status(exp),
                        "expires_at": (
                            datetime.fromtimestamp(exp, tz=UTC).isoformat()
                            if exp is not None
                            else None
                        ),
                        "ttl_remaining_seconds": info.get("ttl_remaining", 0),
                        "can_refresh": "refresh_token" in tokens,
                    }
                )
        return result


token_health_service = TokenHealthService()
