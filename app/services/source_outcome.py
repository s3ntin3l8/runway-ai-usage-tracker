"""How one credential-source attempt is classified (leaf module: imports nothing from the app).

The one rule behind both failover's health bookkeeping (``collector_manager``) and the
on-demand probe (``source_probe``). Kept apart so neither has to import the other.
"""

from __future__ import annotations

from typing import Any

_AUTH_ERROR_TYPES = {"auth_failed", "invalid_api_key"}


def has_usable_card(result: list[dict[str, Any]]) -> bool:
    return any(
        card.get("data_source") != "error"
        and card.get("remaining") != "ERR"
        and not card.get("error_type")
        for card in result
    )


def source_outcome(result: list[dict[str, Any]], auth_rejected: bool, empty_allowed: bool) -> str:
    """Classify one source attempt: ``healthy`` | ``degraded`` | ``auth_failed`` | ``unavailable``.

    The one rule behind both failover's health bookkeeping and the on-demand probe. A fetch
    that still produced usable cards is not failed (an optional request or a refresh retry may
    401 while quota is collected: ``degraded``); a failed one is ``auth_failed`` when the
    provider rejected the credential, else ``unavailable``.
    """
    failed = (not result and not empty_allowed) or bool(result and not has_usable_card(result))
    rejected = auth_rejected or any(card.get("error_type") in _AUTH_ERROR_TYPES for card in result)
    if failed:
        return "auth_failed" if rejected else "unavailable"
    return "degraded" if auth_rejected else "healthy"
