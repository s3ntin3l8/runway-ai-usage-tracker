"""
Base collector class for all AI provider quota collectors.

This module defines the abstract interface that all provider-specific collectors
must implement. Each collector follows a 3-tier fallback pattern:
1. Primary Strategy: Direct API calls (OAuth, REST API, etc.)
2. Secondary Strategy: Local log parsing (CLI logs, cache files, etc.)
3. Tertiary Strategy: Error cards or graceful degradation
"""

import asyncio
import hashlib
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import httpx
from pydantic import ValidationError

from app.core.date_utils import normalize_iso_date

logger = logging.getLogger(__name__)


class WindowType(StrEnum):
    """Canonical reset cadences. New collector code should reference these
    members instead of bare strings; existing string-typed call sites stay
    valid because StrEnum members compare and hash equal to their values."""

    SESSION = "session"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    ROLLING = "rolling"
    UNKNOWN = "unknown"


class ErrorType(StrEnum):
    """Canonical error categories produced by collectors. Same StrEnum
    discipline as WindowType — adopt member references over time, but
    legacy string literals (`error_type="rate_limited"`) keep working."""

    RATE_LIMITED = "rate_limited"
    AUTH_FAILED = "auth_failed"
    API_ERROR = "api_error"
    TIMEOUT = "timeout"
    PARSE_ERROR = "parse_error"
    MISSING_CONFIG = "missing_config"
    UNKNOWN = "unknown"


# Back-compat aliases derived from the enums — single source of truth.
WINDOW_TYPES: frozenset[str] = frozenset(WindowType)


def normalize_account_id(identity: str | None) -> str:
    """
    Derive a stable account_id from a discovered identity (email, subject, etc).

    - Email-shaped identities: lowercase + strip whitespace.
    - Other strings: lowercase + strip; opaque/long values get hashed to 12 hex chars.
    - None / empty → "default" (single-account fallback).

    The same identity always produces the same id. Used by collectors that have
    real per-account discovery so multi-account installs don't collide on "default".
    """
    if not identity:
        return "default"
    s = identity.strip().lower()
    if not s:
        return "default"
    if "@" in s and "." in s.split("@", 1)[1]:
        return s
    if len(s) <= 32 and re.fullmatch(r"[a-z0-9._\-]+", s):
        return s
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]


class BaseCollector(ABC):
    """
    Abstract base class for all AI provider quota collectors.
    Now supports multi-account isolation.
    """

    # Subclasses override these to auto-populate Phase 0B fields on every card.
    PROVIDER_ID: str = "unknown"
    DEFAULT_WINDOW_TYPE: str = "unknown"

    # Subclasses may declare their available strategies as an ordered dict:
    # { "strategy_id": ("Human-Readable Label", "_method_name") }
    # Or with options: { "strategy_id": ("Label", "_method", {"enrich": True}) }
    # The ORDER of items defines the default execution priority.
    # Use {"enrich": True} for strategies that add data to primary results (e.g., local token logs)
    STRATEGIES: dict[str, tuple[str, str] | tuple[str, str, dict]] = {}

    # Standard Data Source Labels
    DATA_SOURCE_API = "api"  # Official API / OAuth
    DATA_SOURCE_WEB = "web"  # Unofficial / Cookie / Scraped
    DATA_SOURCE_LOCAL = "local"  # Log files / CLI / Fast path

    # Standard Input Source Labels (Credentials Origin)
    INPUT_SOURCE_CONFIG = "config"  # Entered via Runway UI
    INPUT_SOURCE_SIDECAR = "sidecar"  # Pushed from remote agent
    INPUT_SOURCE_SERVER = "server"  # Found in local .env or files

    # Error type prioritization for surfacing the most informative error.
    # Higher value = higher priority. Keyed by ErrorType but lookups with
    # plain strings still work because str-Enum members hash like their values.
    ERROR_PRIORITY: dict[ErrorType, int] = {
        ErrorType.RATE_LIMITED: 100,
        ErrorType.AUTH_FAILED: 80,
        ErrorType.API_ERROR: 60,
        ErrorType.TIMEOUT: 40,
        ErrorType.PARSE_ERROR: 20,
        ErrorType.MISSING_CONFIG: 10,
        ErrorType.UNKNOWN: 0,
    }

    def __init__(self, account_id: str | None = None, account_label: str | None = None):
        """
        Initialize BaseCollector.

        Args:
            account_id: Unique identifier for the account (None for default/ENV)
            account_label: Human-readable account label (e.g. email)
        """
        self.account_id = account_id
        self.account_label = account_label
        self._current_input_source: str = self.INPUT_SOURCE_SERVER  # Default for static ENV configs
        # In-memory cache for discovered account labels to avoid redundant regex processing
        self._account_label_cache: str | None = account_label
        # User-supplied strategy ordering/toggles (list of {"id": str, "enabled": bool}).
        # None = use default STRATEGIES ordering with all enabled.
        self._user_strategies: list[dict] | None = None
        # Default timeout for network requests (can be overridden by subclasses)
        self.timeout: float = 10.0

    def apply_strategy_config(self, strategies: list[dict] | None) -> None:
        """
        Accept a user-supplied strategy ordering/enable list from the DB.
        Each entry: {"id": "web", "enabled": True}
        """
        self._user_strategies = strategies

    def _get_strategy_options(self, strategy_id: str) -> dict:
        """Get options for a strategy, or empty dict if not specified."""
        entry = self.STRATEGIES.get(strategy_id)
        if entry and len(entry) == 3:
            return entry[2]
        return {}

    def _resolve_strategies(
        self,
    ) -> list[tuple[Callable[[httpx.AsyncClient], Awaitable[list[dict[str, Any]]]], str]]:
        """
        Resolve the ordered, filtered strategy list to execute.

        If no STRATEGIES are declared (legacy collector), falls back to the
        abstract _primary_strategy + _fallback_strategies pattern.

        Returns a flat ordered list of (callable, strategy_id) tuples.
        """
        if not self.STRATEGIES:
            # Legacy path: no STRATEGIES dict declared — use abstract methods
            return []

        # Build ordered list from STRATEGIES, respecting user overrides
        if self._user_strategies:
            # User has a custom ordering: iterate in their order, respecting enabled flag
            ordered_ids = [s["id"] for s in self._user_strategies if s.get("enabled", True)]
            # Append any strategies not mentioned by the user (new ones added after config)
            known_ids = {s["id"] for s in self._user_strategies}
            for s_id in self.STRATEGIES:
                if s_id not in known_ids:
                    ordered_ids.append(s_id)
        else:
            # Default: use declaration order with all enabled
            ordered_ids = list(self.STRATEGIES.keys())

        # Resolve method names to bound callables, keeping strategy_id for options lookup
        callables = []
        for s_id in ordered_ids:
            if s_id not in self.STRATEGIES:
                continue
            entry = self.STRATEGIES[s_id]
            method_name = entry[1]
            method = getattr(self, method_name, None)
            if callable(method):
                callables.append((method, s_id))
            else:
                logger.warning(
                    f"Strategy method '{method_name}' not found on {self.__class__.__name__}"
                )

        return callables

    async def is_configured(self) -> bool:
        """
        Check if the collector has the necessary credentials/configuration to run.
        Subclasses should override this to check for API keys, session tokens, etc.
        If return False, SmartCollector will skip collection and return empty results
        instead of showing an error card.
        """
        return True

    def _is_valid_credential(self, value: str | None) -> bool:
        """
        Check if a credential value is actually a valid key/token rather than a placeholder.
        Rejects:
        - None or empty strings
        - Values starting with '#' (unparsed .env comments)
        - Values containing common placeholder symbols like '→' or '[UI]'
        """
        if not value:
            return False

        stripped = value.strip()
        if not stripped:
            return False

        # Reject values starting with # (parsed from .env comments)
        if stripped.startswith("#"):
            return False

        # Reject obvious placeholders from .env templates
        placeholders = ["→", "[UI]", "Dashboard", "Settings", "placeholder"]
        if any(p in stripped for p in placeholders):
            return False

        return True

    def _is_error_result(self, results: list[dict[str, Any]]) -> bool:
        """Return True if results are empty or contain an error card."""
        return not results or any(r.get("remaining") == "ERR" for r in results)

    async def collect(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """
        Orchestrate collection strategy with fallbacks and enrichment.
        Automatically tags results with account identifiers.

        If STRATEGIES is declared, uses the dynamic ordered list.
        Otherwise falls back to the abstract _primary_strategy / _fallback_strategies.

        Enrichment: Strategies marked with {"enrich": True} run AFTER primary success
        and merge their data into primary results instead of replacing them.
        """
        try:
            dynamic_strategies = self._resolve_strategies()
            best_error_card = None

            def update_best_error(error_results):
                nonlocal best_error_card
                if not error_results:
                    return
                err_card = error_results[0]
                err_type = err_card.get("error_type", "unknown")
                prio = self.ERROR_PRIORITY.get(err_type, 0)
                best_prio = (
                    self.ERROR_PRIORITY.get(best_error_card.get("error_type", "unknown"), -1)
                    if best_error_card
                    else -1
                )
                if prio > best_prio:
                    best_error_card = err_card

            if dynamic_strategies:
                # Separate primary vs enrichment strategies
                primary_strategies = []
                enrich_strategies = []

                for strategy, s_id in dynamic_strategies:
                    opts = self._get_strategy_options(s_id)
                    if opts.get("enrich"):
                        enrich_strategies.append((strategy, s_id))
                    else:
                        primary_strategies.append((strategy, s_id))

                # Run primary strategies (first success wins)
                results = None

                for strategy, s_id in primary_strategies:
                    try:
                        results = await strategy(client)
                        if not self._is_error_result(results):
                            break
                        update_best_error(results)
                    except Exception as e:
                        logger.warning(f"Strategy {s_id} failed: {e}")
                        self._record_strategy_error(e)

                # If no strategy succeeded, try the error handler
                if self._is_error_result(results):
                    if best_error_card:
                        results = [best_error_card]
                    else:
                        results = await self._error_handler()

                # Capture metadata from primary results so enrichment can align
                # its window boundaries (e.g. actual reset_at vs fixed cutoffs).
                if results and not self._is_error_result(results):
                    self._capture_primary_metadata(results)

                # Run enrichment strategies and merge
                for strategy, s_id in enrich_strategies:
                    try:
                        enrich_data = await strategy(client)
                        if enrich_data and not self._is_error_result(enrich_data):
                            results = self._enrich_results(results, enrich_data)
                    except Exception as e:
                        logger.debug(f"Enrichment strategy {s_id} failed: {e}")

                return self._tag_results(results)

            # Legacy path: primary + fallback list

            # 1. Try Primary Strategy
            results = await self._primary_strategy(client)
            if not self._is_error_result(results):
                return self._tag_results(results)
            update_best_error(results)

            # 2. Try Fallbacks
            for strategy in self._fallback_strategies():
                try:
                    results = await strategy(client)
                    if not self._is_error_result(results):
                        return self._tag_results(results)
                    update_best_error(results)
                except Exception as e:
                    logger.warning(f"Fallback strategy failed: {e}")

            # 3. All failed
            if best_error_card:
                return self._tag_results([best_error_card])
            return self._tag_results(await self._error_handler())

        except Exception as e:
            logger.error(f"Collector {self.__class__.__name__} failed: {e}")
            return self._tag_results(
                [
                    {
                        "service_name": "Collector Error",
                        "icon": "⚠️",
                        "remaining": "ERR",
                        "unit": "fail",
                        "reset": "—",
                        "pace": "Stopped",
                        "detail": f"Internal Error: {str(e)[:30]}",
                        "health": "critical",
                    }
                ]
            )

    def _enrich_results(
        self,
        primary: list[dict[str, Any]] | None,
        enrichment: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        Merge enrichment data into primary results.

        Enrichment strategies provide token breakdowns, session counts, and
        detail suffixes only. They never act as fallback for missing primary
        quota data.

        Matching is performed by (service_name, variant, window_type, model_id)
        with graceful fallback to less specific keys. Enrichment dicts with
        model_id=None act as aggregate fallbacks for any primary card.
        Canonical fields injected: token_usage, by_model, msgs.
        pct_used is derived from the primary card's own used_value / limit_value.
        """
        if not primary or self._is_error_result(primary):
            return primary or []

        if self._is_error_result(enrichment):
            return primary

        # Index enrichment by multiple specificity levels so lookups succeed
        # even when enrichment dicts omit service_name or variant.
        # model_id is included as a 4th dimension; None means "aggregate".
        by_key: dict[tuple[str | None, str | None, str | None, str | None], dict[str, Any]] = {}
        for e in enrichment:
            if not e.get("_enrichment_detail"):
                continue
            sn = e.get("service_name")
            va = e.get("variant")
            wt = e.get("window_type")
            mid = e.get("model_id")
            # If model_id is set, it MUST be part of every key to prevent
            # specific model enrichment from polluting the generic fallback.
            if mid is not None:
                keys = [
                    (sn, va, wt, mid),
                    (sn, va, None, mid),
                    (sn, None, wt, mid),
                    (None, va, wt, mid),
                    (sn, None, None, mid),
                    (None, va, None, mid),
                    (None, None, wt, mid),
                    (None, None, None, mid),
                ]
            else:
                keys = [
                    (sn, va, wt, None),
                    (sn, va, None, None),
                    (sn, None, wt, None),
                    (None, va, wt, None),
                    (sn, None, None, None),
                    (None, va, None, None),
                    (None, None, wt, None),
                    (None, None, None, None),
                ]
            for key in keys:
                if key not in by_key:
                    by_key[key] = e

        for card in primary:
            card_sn = card.get("service_name")
            card_va = card.get("variant")
            card_wt = card.get("window_type")
            card_mid = card.get("model_id")
            match = None

            # Build search order. If card has a model_id, we MUST match an enrichment with that same model_id.
            # We never fall back from a specific model to an aggregate enrichment (model_id=None).
            search_keys = [
                (card_sn, card_va, card_wt, card_mid),
                (card_sn, card_va, None, card_mid),
                (card_sn, None, card_wt, card_mid),
                (None, card_va, card_wt, card_mid),
                (card_sn, None, None, card_mid),
                (None, card_va, None, card_mid),
                (None, None, card_wt, card_mid),
                (None, None, None, card_mid),
            ]

            for key in search_keys:
                match = by_key.get(key)
                if match:
                    break

            if not match:
                continue

            # Inject canonical enrichment fields
            for field in ("token_usage", "by_model", "msgs"):
                if field in match and match[field] is not None:
                    card[field] = match[field]

            # Derive pct_used from the primary card's own quota data
            used = card.get("used_value")
            limit = card.get("limit_value")
            if used is not None and limit and limit > 0:
                card["pct_used"] = (used / limit) * 100

            # Append detail suffix
            suffix = match.get("_enrichment_detail", "")
            if suffix:
                current = card.get("detail", "").rstrip()
                card["detail"] = f"{current} | {suffix}".strip(" |")

        return primary

    def _tag_results(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Add account identifiers to every card in the result list.
        Also attempts to discover account_label if it is missing by scanning card details.
        """
        if not results:
            return []

        # Phase 0C: Auto-discover account label from detail if not already set by user
        # Avoid running discovery if a custom label is already present or cached.
        if not self._account_label_cache or self._account_label_cache.lower() == "default":
            discovered_label = None
            for card in results:
                detail = card.get("detail", "")
                if not detail:
                    continue

                # Look for email patterns in detail text
                match = re.search(r"([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})", detail)
                if match:
                    discovered_label = match.group(1)
                    break

                # Fallback to org pattern or standalone username after separator (· or |)
                org_match = re.search(r"org:\s*([^\s·\[\]|]+)", detail)
                if org_match:
                    discovered_label = f"org: {org_match.group(1)}"
                    break

                # Standalone username after a dot/separator
                user_match = re.search(
                    r"[·|]\s*([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}|[a-zA-Z0-9_-]+)(?:\s*\[[^\]]+\])?$",
                    detail,
                )
                if user_match:
                    discovered_label = user_match.group(1)
                    break

            if discovered_label:
                self._account_label_cache = discovered_label
                self.account_label = discovered_label

        # 2. Tag cards
        tagged_results = []
        from app.models.schemas import LimitCard

        for card_data in results:
            if not isinstance(card_data, dict):
                logger.error(f"Collector returned non-dict card: {type(card_data)}")
                continue

            # Phase 0B: inject provider_id and window_type from class constants
            if "provider_id" not in card_data or not card_data.get("provider_id"):
                card_data["provider_id"] = self.PROVIDER_ID
            if "window_type" not in card_data or card_data.get("window_type") == "unknown":
                if self.DEFAULT_WINDOW_TYPE != "unknown":
                    card_data["window_type"] = self.DEFAULT_WINDOW_TYPE
            # Enforce the canonical window_type enum: surface stale values loudly.
            wt = card_data.get("window_type", "unknown")
            if wt not in WINDOW_TYPES:
                # window_type is a short enum string from card_data, not a credential.
                logger.warning(
                    "%s emitted card with non-canonical window_type=%r; coercing to 'unknown'",
                    self.PROVIDER_ID,
                    wt,
                )
                card_data["window_type"] = "unknown"

            # Tag account identifiers
            if "account_id" not in card_data or not card_data.get("account_id"):
                card_data["account_id"] = self.account_id or "default"
            if (
                "account_label" not in card_data
                or not card_data.get("account_label")
                or card_data.get("account_label").lower() == "default"
            ):
                card_data["account_label"] = (
                    self.account_label or self._account_label_cache or "Default"
                )

            # Tag input source (origin of credentials)
            if "input_source" not in card_data or card_data.get("input_source") == "unknown":
                card_data["input_source"] = getattr(self, "_current_input_source", "unknown")

            # Phase 1: Ensure timestamps are timezone-aware ISO strings
            now_iso = datetime.now(UTC).isoformat()
            if "updated_at" not in card_data or not card_data.get("updated_at"):
                card_data["updated_at"] = now_iso
            else:
                card_data["updated_at"] = normalize_iso_date(card_data["updated_at"])

            if "reset_at" in card_data:
                card_data["reset_at"] = normalize_iso_date(card_data["reset_at"])

            # VALIDATION: Ensure card matches our public schema before returning
            try:
                valid_card = LimitCard(**card_data)
                tagged_results.append(valid_card.model_dump())
            except ValidationError as e:
                # Drop the card and log loudly — invalid cards must never reach
                # downstream consumers (rollups, webhooks, the dashboard).
                logger.error(f"Schema validation failed for {self.PROVIDER_ID} card: {e}")

        # Deduplicate by composite key (service_name, window_type, variant, model_id).
        # Keep the first occurrence and log any duplicates as a safety net
        # against parser bugs across all collectors.
        seen_keys: set[tuple[str, str, str | None, str | None]] = set()
        deduped: list[dict[str, Any]] = []
        for card in tagged_results:
            key = (
                card.get("service_name", ""),
                card.get("window_type", "unknown"),
                card.get("variant"),
                card.get("model_id"),
            )
            if key in seen_keys:
                logger.warning(
                    "Dropping duplicate card from %s: %s (window_type=%s, variant=%s, model_id=%s)",
                    self.PROVIDER_ID,
                    key[0],
                    key[1],
                    key[2],
                    key[3],
                )
                continue
            seen_keys.add(key)
            deduped.append(card)

        return deduped

    def _capture_primary_metadata(self, primary: list[dict[str, Any]]) -> None:
        """
        Hook called after primary strategies succeed and before enrichment runs.
        Subclasses override to extract metadata (e.g. reset_at, tier) from
        primary cards so enrichment strategies can align window boundaries.
        """
        pass

    @abstractmethod
    async def _primary_strategy(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Execute the primary (usually API) collection strategy."""
        pass

    @abstractmethod
    def _fallback_strategies(
        self,
    ) -> list[Callable[[httpx.AsyncClient], Awaitable[list[dict[str, Any]]]]]:
        """Return an ordered list of fallback async methods to execute."""
        pass

    @abstractmethod
    async def _error_handler(self) -> list[dict[str, Any]]:
        """Return the error card(s) when all strategies fail."""
        pass

    async def reset(self):
        """Reset collector state (e.g., terminal failures). Subclasses should override if needed."""
        pass

    def _record_strategy_error(self, exc: Exception) -> None:
        """Emit a kind=error UsageEvent for HTTP/timeout failures during strategy execution.

        Best-effort: swallows all errors so it never disrupts the collector flow.
        Only fires for actionable provider-side errors (rate_limit, auth_failed, timeout).
        """
        try:
            reason: str | None = None
            if isinstance(exc, httpx.HTTPStatusError):
                status = exc.response.status_code
                if status == 429:
                    reason = "rate_limit"
                elif status in (401, 403):
                    reason = "auth_failed"
                elif status == 503:
                    reason = "quota_exceeded"
            elif isinstance(exc, httpx.TimeoutException | asyncio.TimeoutError):
                reason = "timeout"
            elif isinstance(exc, httpx.NetworkError):
                reason = "network"

            if reason is None:
                return  # non-actionable error; don't flood the table

            from sqlmodel import Session

            from app.core.db import engine
            from app.services.error_events import record_provider_error

            account = getattr(self, "account_id", None) or "default"
            with Session(engine) as err_session:
                record_provider_error(
                    err_session,
                    provider_id=self.PROVIDER_ID,
                    account_id=account,
                    reason=reason,
                    detail=str(exc)[:500],
                )
        except Exception:
            pass  # never let error recording break the collector

    def handle_429(
        self, response: httpx.Response | None = None, retry_after: float | None = None
    ) -> list[dict[str, Any]]:
        """
        Optional hook for provider-specific 429 (rate limit) handling.

        Called by SmartCollector when a 429 response is detected.
        Subclasses may override to implement custom backoff logic
        (e.g., token rotation for Anthropic OAuth).

        Args:
            response: The HTTP response that triggered the 429, if available.
            retry_after: Seconds to wait before retrying, if known.

        Returns:
            An error card list, or empty list to let SmartCollector handle it.
        """
        return []


def format_token_details(tokens: dict[str, int]) -> str:
    """
    Format token counts uniformly for display across all collectors.
    Expected format: in:1.2k, out:500, cached:1M, reasoning:100
    """

    def _fmt(n: int) -> str:
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        if n >= 1000:
            return f"{n / 1000:.1f}k"
        return str(n)

    parts = []
    if tokens.get("input"):
        parts.append(f"in:{_fmt(tokens['input'])}")
    if tokens.get("output"):
        parts.append(f"out:{_fmt(tokens['output'])}")
    if tokens.get("cache_read"):
        parts.append(f"cached:{_fmt(tokens['cache_read'])}")
    if tokens.get("reasoning"):
        parts.append(f"reasoning:{_fmt(tokens['reasoning'])}")

    total = tokens.get("total", 0)
    if not total:
        total = tokens.get("input", 0) + tokens.get("output", 0)

    if parts:
        return ", ".join(parts)
    return f"{_fmt(total)} tokens"
