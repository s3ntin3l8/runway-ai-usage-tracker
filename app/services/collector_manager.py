"""
Manages collection of AI provider quotas with smart differential fetching.

This module orchestrates all collectors and wraps them with SmartCollector
for intelligent caching to reduce API calls while maintaining fresh data.
Now supports multi-account dynamic spawning based on discovered tokens.
"""

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.utils import IdentityExtractor, has_refresh_credential, scrub_log
from app.services.collectors.anthropic import AnthropicCollector
from app.services.collectors.antigravity import AntigravityCollector
from app.services.collectors.chatgpt import ChatGPTCollector
from app.services.collectors.deepseek import DeepSeekCollector
from app.services.collectors.gemini import GeminiCollector
from app.services.collectors.github import GitHubCollector
from app.services.collectors.kimi_api import KimiApiCollector
from app.services.collectors.kimi_coding import KimiCodingCollector
from app.services.collectors.kimi_k2 import KimiK2Collector
from app.services.collectors.minimax import MiniMaxCollector
from app.services.collectors.ollama import OllamaCollector
from app.services.collectors.opencode import OpenCodeCollector
from app.services.collectors.openrouter import OpenRouterCollector
from app.services.collectors.xai import XaiCollector
from app.services.collectors.zai import ZaiCollector
from app.services.credential_sources import is_sidecar_source
from app.services.refresh_policy import machine_owns_credential
from app.services.smart_collector import SmartCollector
from app.services.source_outcome import source_outcome
from app.services.token_cache import token_cache

logger = logging.getLogger(__name__)


IDENTITY_VERIFIER_SUFFIX = ":identity-pending"
# Pending sources verified per provider per cycle (oldest-due first), so a host reporting
# many unidentifiable credentials cannot turn every cycle into a burst of upstream calls.
MAX_VERIFICATIONS_PER_CYCLE = 5
# What ingest calls a push that carries no sidecar id; keys the retry state the same way.
LOCAL_SIDECAR_ID = "local"


def verifier_key(provider_id: str) -> str:
    """Smart-collector key of a provider's identity-verification run."""
    return f"{provider_id}:default{IDENTITY_VERIFIER_SUFFIX}"


def is_verifier_key(key: str) -> bool:
    """Whether a smart-collector key is an identity-verification run (not a credential)."""
    return key.endswith(IDENTITY_VERIFIER_SUFFIX)


class CollectorManager:
    """
    Manages collection of all AI provider quotas with support for multiple accounts.

    Dynamically spawns SmartCollector instances for:
    1. Default/Static accounts (configured via ENV)
    2. Dynamic accounts (ingested from sidecars via token_cache)
    """

    def __init__(self):
        """Initialize collector registry."""
        self._sync_lock = asyncio.Lock()
        # Registry of available collector classes and their default settings
        self.collector_registry = {
            "anthropic": (AnthropicCollector, "Claude (Anthropic)", 900),
            "antigravity": (AntigravityCollector, "Antigravity", 900),
            "gemini": (GeminiCollector, "Gemini", 900),
            "github": (GitHubCollector, "GitHub Copilot", 900),
            "chatgpt": (ChatGPTCollector, "ChatGPT", 900),
            "opencode": (OpenCodeCollector, "OpenCode", 900),
            "zai": (ZaiCollector, "zAI", 900),
            "kimi_api": (KimiApiCollector, "Kimi API", 900),
            "kimi_coding": (KimiCodingCollector, "Kimi Coding", 900),
            "kimi_k2": (KimiK2Collector, "Kimi K2", 900),
            "openrouter": (OpenRouterCollector, "OpenRouter", 900),
            "deepseek": (DeepSeekCollector, "DeepSeek", 900),
            "minimax": (MiniMaxCollector, "MiniMax", 900),
            "ollama": (OllamaCollector, "Ollama Cloud", 900),
            "xai": (XaiCollector, "xAI (Grok)", 900),
        }

        # Active collectors keyed by "provider_id:account_id"
        self.smart_collectors: dict[str, SmartCollector] = {}
        self._client = None
        self._last_sync_time: float = 0.0
        self._credential_source_preferences: dict[tuple[str, str], dict[str, tuple[bool, int]]] = {}
        # (provider, account) -> source_id -> (last health, next_retry_at): what failover needs
        # to push a rejected credential behind working ones and rest it between retries.
        self._credential_source_state: dict[
            tuple[str, str], dict[str, tuple[str, datetime | None]]
        ] = {}
        self._collect_lock = asyncio.Lock()
        self._collect_future: asyncio.Future | None = None
        self.last_collection_outcomes: list[dict[str, Any]] = []
        # Concurrency limit: max 10 collectors running at once
        self._semaphore = asyncio.Semaphore(10)

        logger.info(
            f"CollectorManager initialized with {len(self.collector_registry)} registered providers"
        )

    async def _sync_collectors(  # noqa: PLR0915 — known-debt: smart-collector lifecycle, refactor tracked separately
        self, *, force: bool = False
    ):
        """Synchronize active SmartCollectors with discovered accounts.

        Throttled to run at most once every 60 seconds to avoid redundant
        TokenCache lookups on every /api/limits request. Config mutations
        pass ``force=True`` so an enable/disable takes effect immediately
        instead of waiting out the throttle.
        """
        if not force and time.time() - self._last_sync_time < 60.0:
            return
        async with self._sync_lock:
            # Re-check inside lock to avoid double-sync when multiple requests
            # are waiting on the lock simultaneously.
            if not force and time.time() - self._last_sync_time < 60.0:
                return
            # Load DB provider configs: provider_id -> account_id -> ProviderConfig
            # We use a nested map to handle multi-account overrides correctly.
            db_configs: dict[str, dict[str, Any]] = {}
            global_poll_interval: int | None = None
            source_preferences: dict[tuple[str, str], dict[str, tuple[bool, int]]] = {}
            source_state: dict[tuple[str, str], dict[str, tuple[str, datetime | None]]] = {}
            try:
                from sqlmodel import Session
                from sqlmodel import select as sqlselect

                from app.core.db import engine
                from app.models.db import CredentialSource, ProviderConfig, SystemConfig

                with Session(engine) as _s:
                    for r in _s.exec(sqlselect(ProviderConfig)).all():
                        if r.provider_id not in db_configs:
                            db_configs[r.provider_id] = {}
                        # Keep a detached value snapshot. The session commits
                        # and closes below; retaining ORM instances here makes
                        # later attribute reads fail when expire_on_commit is
                        # enabled (and can trigger implicit DB access).
                        from types import SimpleNamespace

                        db_configs[r.provider_id][r.account_id] = SimpleNamespace(
                            enabled=r.enabled,
                            archived=r.archived,
                            poll_interval_seconds=r.poll_interval_seconds,
                            account_label=r.account_label,
                            strategies=r.strategies,
                        )

                        # Sync manual tokens to cache to survive reloads/restarts
                        if r.enabled and (r.api_key or r.session_cookie):
                            await self._sync_manual_config_to_cache(r, _s)

                    for row in _s.exec(sqlselect(CredentialSource)).all():
                        account_sources = source_preferences.setdefault(
                            (row.provider_id, row.account_id), {}
                        )
                        account_sources[row.source_id] = (row.enabled, row.priority)
                        source_state.setdefault((row.provider_id, row.account_id), {})[
                            row.source_id
                        ] = (row.health, row.next_retry_at)

                    sys_cfg = _s.exec(sqlselect(SystemConfig)).first()
                    if sys_cfg and sys_cfg.default_poll_interval_seconds:
                        global_poll_interval = sys_cfg.default_poll_interval_seconds

                    _s.commit()

            except Exception as e:
                logger.debug(f"Could not load provider configs from DB: {e}")
            # A saved configuration is an explicit opt-in boundary. If every
            # account is disabled or archived, credentials arriving from a
            # sidecar must not silently reactivate collection under a newly
            # discovered identity.
            inactive_providers = {
                provider_id
                for provider_id, accounts in db_configs.items()
                if accounts
                and not any(cfg.enabled and not cfg.archived for cfg in accounts.values())
            }
            self._credential_source_preferences = source_preferences
            self._credential_source_state = source_state
            await self.reconcile_token_cache_from_durable_tags()

            # 1. Ensure Default/Static collectors are present
            for p_id, (cls, name, ttl) in self.collector_registry.items():
                if p_id in inactive_providers:
                    self.smart_collectors.pop(f"{p_id}:default", None)
                    continue
                # For default collector, we look for account_id="default"
                provider_acc_configs = db_configs.get(p_id, {})
                db_cfg = provider_acc_configs.get("default")

                if db_cfg is not None and not db_cfg.enabled:
                    # Remove existing collector if it was previously running
                    self.smart_collectors.pop(f"{p_id}:default", None)
                    continue
                if db_cfg is None and provider_acc_configs:
                    # Config rows exist but none is the "default" sentinel —
                    # collection is account-keyed (wizard-created emails).
                    # Spawning a blanket default here would shadow step 2's
                    # per-account `enabled` checks and keep collecting after
                    # the user disables the only account.
                    self.smart_collectors.pop(f"{p_id}:default", None)
                    continue
                effective_ttl = (
                    db_cfg.poll_interval_seconds
                    if db_cfg and db_cfg.poll_interval_seconds
                    else global_poll_interval or ttl
                )
                key = f"{p_id}:default"
                db_label = db_cfg.account_label if db_cfg else None
                if key not in self.smart_collectors:
                    logger.info(f"Spawning default collector for {p_id}")
                    collector_instance = cls(account_label=db_label)
                    if not collector_instance.CREDENTIALS_KEYED_BY_ACCOUNT_ID:
                        # Display identity may be resolved during collection,
                        # but credentials and auth-failure tracking remain on
                        # the default source slot.
                        collector_instance.credential_account_id = "default"
                    # Apply user strategy ordering/toggles if configured
                    if db_cfg and db_cfg.strategies:
                        collector_instance.apply_strategy_config(db_cfg.strategies)
                    self.smart_collectors[key] = SmartCollector(
                        collector=collector_instance,
                        collector_name=name,
                        ttl=effective_ttl,
                    )
                else:
                    sc = self.smart_collectors[key]
                    if effective_ttl != sc.ttl:
                        sc.ttl = effective_ttl
                    # Propagate updated account_label from ProviderConfig
                    if sc.collector.account_label != db_label:
                        sc.collector.account_label = db_label
                    # Propagate updated strategy config
                    new_strategies = db_cfg.strategies if db_cfg else None
                    if sc.collector._user_strategies != new_strategies:
                        sc.collector.apply_strategy_config(new_strategies)

            # 2. Discover active dynamic collectors from TokenCache
            active_accounts = await token_cache.get_all_active_accounts()
            active_keys = set()

            # Identity-pending sources are intentionally absent from the
            # aggregate account cache so they cannot render as "default".
            # Run a dedicated verifier regardless of whether a normal default
            # collector exists: that collector may already have a resolved
            # display identity and therefore read another credential slot.
            for p_id, (cls, name, ttl) in self.collector_registry.items():
                if p_id in inactive_providers:
                    self.smart_collectors.pop(verifier_key(p_id), None)
                    continue
                pending_sources = await token_cache.get_pending_sources(p_id)
                has_pending_source = any(
                    is_sidecar_source(source)
                    and source.get("credential_origin")
                    and source.get("identity_pending") is True
                    for source in pending_sources
                )
                key = verifier_key(p_id)
                if not has_pending_source:
                    self.smart_collectors.pop(key, None)
                    continue
                active_keys.add(key)
                if key in self.smart_collectors:
                    continue
                full_name = f"{name} (identity pending)"
                collector_instance = cls(account_id="default")
                collector_instance.credential_account_id = "default"
                self.smart_collectors[key] = SmartCollector(
                    collector=collector_instance,
                    collector_name=full_name,
                    ttl=ttl,
                )
                logger.info("Spawning identity-verification collector for %s", p_id)

            for p_id, acc_id, acc_name in active_accounts:
                if p_id in self.collector_registry:
                    if p_id in inactive_providers:
                        self.smart_collectors.pop(f"{p_id}:{acc_id}", None)
                        continue
                    default_key = f"{p_id}:default"
                    default_collector = self.smart_collectors.get(default_key)
                    # Skip only the cache entry whose slot the default collector
                    # actually reads. Its display identity can differ from that
                    # credential slot when LatestUsage restores a durable id.
                    default_credential_account_id = None
                    if default_collector is not None:
                        default_credential_account_id = (
                            getattr(
                                default_collector.collector,
                                "credential_account_id",
                                None,
                            )
                            or default_collector.collector.account_id
                            or "default"
                        )
                    if default_collector is not None and acc_id == default_credential_account_id:
                        continue
                    cls, name, ttl = self.collector_registry[p_id]

                    # For dynamic account, check for specific override OR fallback to default provider override
                    provider_acc_configs = db_configs.get(p_id, {})
                    if (
                        acc_id == "default"
                        and provider_acc_configs
                        and "default" not in provider_acc_configs
                    ):
                        # Config rows exist but none is the default sentinel —
                        # collection is account-keyed. The sidecar can still
                        # stamp a literal "default" into the token cache
                        # (`_gemini_account_email` / `_ag_account_email`
                        # fallbacks); spawning {pid}:default here would keep
                        # collecting after the user disabled the only account.
                        self.smart_collectors.pop(f"{p_id}:default", None)
                        continue
                    own_cfg = provider_acc_configs.get(acc_id)
                    db_cfg = own_cfg or provider_acc_configs.get("default")

                    if db_cfg is not None and not db_cfg.enabled:
                        # Remove existing dynamic collector and its stale cards
                        self.smart_collectors.pop(f"{p_id}:{acc_id}", None)
                        continue
                    effective_ttl = (
                        db_cfg.poll_interval_seconds
                        if db_cfg and db_cfg.poll_interval_seconds
                        else global_poll_interval or ttl
                    )

                    # Prioritize the account's *own* DB label, then acc_name from cache.
                    # The provider-wide ``default`` row may supply enabled/poll/strategy
                    # fallbacks, but its label names a different account — borrowing it
                    # stamps this account's cards with someone else's email.
                    db_label = own_cfg.account_label if own_cfg else None
                    final_label = db_label or acc_name

                    key = f"{p_id}:{acc_id}"
                    active_keys.add(key)
                    if key not in self.smart_collectors:
                        full_name = f"{name} ({final_label or acc_id[:6]})"
                        logger.info(f"Spawning dynamic collector for {p_id} account {acc_id}")
                        collector_instance = cls(account_id=acc_id, account_label=final_label)
                        # Apply user strategy ordering/toggles if configured
                        if db_cfg and db_cfg.strategies:
                            collector_instance.apply_strategy_config(db_cfg.strategies)
                        self.smart_collectors[key] = SmartCollector(
                            collector=collector_instance,
                            collector_name=full_name,
                            ttl=effective_ttl,
                        )
                    else:
                        sc = self.smart_collectors[key]
                        if effective_ttl != sc.ttl:
                            sc.ttl = effective_ttl
                        if sc.collector.account_label != final_label:
                            sc.collector.account_label = final_label
                        # Propagate updated strategy config
                        new_strategies = db_cfg.strategies if db_cfg else None
                        if sc.collector._user_strategies != new_strategies:
                            sc.collector.apply_strategy_config(new_strategies)

            # 3. Prune collectors whose accounts disappeared from the token cache
            stale_keys = [
                key
                for key in self.smart_collectors
                if not key.endswith(":default") and key not in active_keys
            ]
            for key in stale_keys:
                logger.info(f"Removing stale collector for {key}")
                self.smart_collectors.pop(key, None)
            self._last_sync_time = time.time()

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create a persistent httpx client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=30.0, event_hooks={"response": [token_cache.observe_response]}
            )
        return self._client

    async def close(self):
        """Close the internal HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _sync_manual_config_to_cache(self, r, session=None):
        """Helper to push a single ProviderConfig into the token cache."""
        from app.core.utils import IdentityExtractor

        all_tokens = {}

        # Handle API Key (OAuth Token)
        if r.api_key:
            # Strip Bearer prefix if present
            token_val = r.api_key
            if token_val.lower().startswith("bearer "):
                token_val = token_val[7:].strip()

            if r.provider_id == "xai":
                # A manually saved xAI token is the access bearer only.
                all_tokens["xai_access"] = token_val
            else:
                all_tokens["oauth_token"] = token_val
                # These collectors read the credential from the api_key slot.
                if r.provider_id in (
                    "opencode",
                    "ollama",
                    "minimax",
                    "kimi_coding",
                    "deepseek",
                    "openrouter",
                    "zai",
                    "kimi_api",
                    "kimi_k2",
                ):
                    all_tokens["api_key"] = token_val
            if r.provider_id == "chatgpt":
                acc_id = IdentityExtractor.get_openai_account_id_from_jwt(token_val)
                if acc_id:
                    all_tokens["account_id"] = acc_id

        # Handle Session Cookie
        if r.session_cookie:
            # Map generic session_cookie to all common provider-specific keys
            all_tokens.update(
                {
                    "session_cookie": r.session_cookie,
                    "cookie_session": r.session_cookie,
                    "cookie_sessionKey": r.session_cookie,
                    "cookie___Secure-next-auth.session-token": r.session_cookie,
                }
            )
            # OpenCode's console handshake uses both auth and console_session.
            # Scope the extra slot to the provider that reads it.
            if r.provider_id == "opencode":
                all_tokens["console_session"] = r.session_cookie

        # Handle oai-sc service-credential cookie (ChatGPT only)
        if r.provider_id == "chatgpt" and r.oai_sc_cookie:
            all_tokens["cookie_oai-sc"] = r.oai_sc_cookie

        if all_tokens:
            source_id = f"config:{r.provider_id}:{r.account_id or 'default'}"
            enabled = True
            priority = 0
            if session is not None:
                from app.services.credential_sources import touch_source

                source = touch_source(
                    session,
                    provider_id=r.provider_id,
                    account_id=r.account_id or "default",
                    source_id=source_id,
                    source_type="config",
                    source_label="Manual configuration",
                )
                enabled = source.enabled
                priority = source.priority
            await token_cache.store(
                r.provider_id,
                all_tokens,
                account_id=r.account_id or "default",
                source="config",
                source_id=source_id,
                source_metadata={
                    "source_type": "config",
                    "source_label": "Manual configuration",
                    "enabled": enabled,
                    "priority": priority,
                },
            )

    async def collect_all(self) -> list[dict[str, Any]]:
        """
        Collect all limits across all active accounts.

        Implements a single-flight pattern: if a collection cycle is already
        in progress, concurrent callers wait for it and share the same result
        instead of triggering redundant parallel collections.

        The lock is held only for the brief check/register of the Future so
        that followers can immediately join the in-flight work rather than
        blocking behind it.
        """
        async with self._collect_lock:
            if self._collect_future is not None and not self._collect_future.done():
                # A collection is already running — grab the future and join it
                future = self._collect_future
                is_leader = False
            else:
                # Register a new future before releasing the lock so any
                # concurrent callers that arrive now will find it and wait
                future = asyncio.get_running_loop().create_future()
                self._collect_future = future
                is_leader = True
        # Lock released; only the leader drives the collection

        if is_leader:
            try:
                result = await self._do_collect()
                if not future.done():
                    future.set_result(result)
            except asyncio.CancelledError:
                if not future.done():
                    future.cancel()
                raise
            except BaseException as e:
                if not future.done():
                    future.set_exception(e)
                raise

        return await future

    async def _do_collect(self) -> list[dict[str, Any]]:
        """Execute one collection cycle across all active collectors."""
        # Ensure we have collectors for all current accounts
        await self._sync_collectors()

        client = await self._get_client()

        active_keys = list(self.smart_collectors.keys())
        # Wrap each in a Task so we can harvest partial results on global timeout.
        # Each task also has its own 25s per-collector timeout in
        # _collect_with_semaphore, so the 45s here is a safety net for the
        # combined latency of the queue under semaphore contention.
        tasks = [
            asyncio.create_task(self._collect_with_semaphore(key, client)) for key in active_keys
        ]
        done, pending = await asyncio.wait(tasks, timeout=45.0)
        if pending:
            logger.error(
                "Global collector timeout reached after 45s: "
                f"{len(pending)} task(s) still running, harvesting {len(done)} completed."
            )
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

        results: list[Any] = []
        for t in tasks:
            if t.cancelled():
                results.append(asyncio.CancelledError())
            elif (exc := t.exception()) is not None:
                results.append(exc)
            else:
                results.append(t.result())

        flattened = []
        outcomes: list[dict[str, Any]] = []
        for i, res in enumerate(results):
            key = active_keys[i]
            smart = self.smart_collectors.get(key)
            provider_id = getattr(getattr(smart, "collector", None), "PROVIDER_ID", None)
            account_id = getattr(getattr(smart, "collector", None), "account_id", None) or "default"
            failed = isinstance(res, (Exception, asyncio.CancelledError))
            state = "failed" if failed else (smart.last_collection_state if smart else "failed")
            # A pending verifier has no proven account identity yet. Its
            # failure must not be attributed to the shared server:<provider>
            # contribution under `default` (which may belong to another
            # credential or an archived account).
            if is_verifier_key(key) and account_id == "default":
                if failed:
                    logger.debug(
                        "Identity-pending collection failed for %s; no account outcome recorded",
                        scrub_log(provider_id),
                    )
                continue
            # The verifier is not the server's own credential: once it has adopted a proven
            # email its state must not be recorded against that account's server outcome.
            if not is_verifier_key(key):
                outcomes.append(
                    {
                        "provider_id": provider_id,
                        "account_id": account_id,
                        # The *contribution* source ("the server collected this card"), which
                        # LatestUsageContribution/accumulator key on. Not a credential id: which
                        # credential answered is stamped on its CredentialSource row
                        # (last_success_at / is_active), see ``_record_source_health``.
                        "source_id": f"server:{provider_id}",
                        "state": state,
                    }
                )
            if failed:
                logger.error(f"Unexpected error from collector {active_keys[i]}: {res}")
                continue
            if isinstance(res, list):
                flattened.extend(res)

        logger.info(
            f"Collected {len(flattened)} total cards from {len(active_keys)} active accounts"
        )
        self.last_collection_outcomes = outcomes
        return flattened

    async def _collect_with_semaphore(
        self, key: str, client: httpx.AsyncClient
    ) -> list[dict[str, Any]]:
        """Run a single collector with semaphore/timeout protection."""
        health_updates: dict[str, str] = {}
        async with self._semaphore:
            collector = self.smart_collectors[key].collector
            provider_id = getattr(collector, "PROVIDER_ID", None)
            account_id = (
                getattr(collector, "credential_account_id", None)
                or getattr(collector, "account_id", None)
                or "default"
            )
            result = await self._collect_with_source_failover(key, client, health_updates)
        # Release the shared semaphore before doing synchronous DB I/O.
        if isinstance(provider_id, str) and isinstance(account_id, str):
            self._record_source_health(provider_id, account_id, health_updates)
            if not health_updates and key.endswith(":default"):
                # No cache-backed source was tried, so the default collector used
                # credentials the server host found itself (env var / local file).
                # A cache hit or skipped collection never exercised the credential, so it
                # must not be stamped as a fresh success (or an attempt): register the row,
                # but only record an outcome when the credential was actually used.
                used = self.smart_collectors[key].last_collection_state not in ("cached", "skipped")
                await asyncio.to_thread(
                    self._record_server_sources,
                    provider_id,
                    getattr(collector, "account_id", None) or "default",
                    self._result_health(
                        result,
                        empty_allowed=bool(getattr(collector, "successful_empty_result", False)),
                    )
                    if used
                    else None,
                )
        return result

    @staticmethod
    def _result_health(result: list[dict[str, Any]], *, empty_allowed: bool = False) -> str:
        """Collapse a collector result into the per-source health vocabulary.

        A collector that legitimately reports nothing (``successful_empty_result``) is not
        failing when it does.
        """
        if not result and empty_allowed:
            return "healthy"
        if any(
            card.get("data_source") != "error"
            and card.get("remaining") != "ERR"
            and not card.get("error_type")
            for card in result
        ):
            return "healthy"
        if any(card.get("error_type") in {"auth_failed", "invalid_api_key"} for card in result):
            return "auth_failed"
        return "unavailable"

    @staticmethod
    def _record_server_sources(provider_id: str, account_id: str, health: str | None) -> None:
        """Register (and stamp) the env/file credentials that actually fed this collection.

        ``health=None`` registers/prunes the rows without recording an outcome (the result
        came from cache or the collection was skipped, so nothing was verified).

        Server-discovered credentials never enter the token cache, so without a row they
        are invisible to the credential views and leave no evidence for data-health
        checks. Best-effort: provenance must never break a collection.
        """
        try:
            from sqlmodel import Session

            from app.core.db import engine
            from app.services.credential_provider import CredentialProvider
            from app.services.credential_sources import (
                prune_server_sources,
                record_source_result,
                register_server_source,
                server_source_id,
            )

            effective = CredentialProvider.get_credentials(provider_id).sources
            origins = [
                origin
                for origin in CredentialProvider.server_credential_origins(provider_id)
                # Runway's own files (the GitHub device-login token) count too: they are
                # credentials the server uses, and evidence for the account they serve.
                if origin["managed"] or any(effective.get(k) == "server" for k in origin["keys"])
            ]
            with Session(engine) as session:
                for origin in origins:
                    row = register_server_source(
                        session,
                        provider_id=provider_id,
                        account_id=account_id,
                        source_type=origin["source_type"],
                        label=origin["label"],
                        token_types=origin["keys"],
                    )
                    if health is not None:
                        record_source_result(row, health)
                    session.add(row)
                prune_server_sources(
                    session,
                    provider_id,
                    {server_source_id(provider_id, o["source_type"], o["label"]) for o in origins},
                )
                session.commit()
        except Exception:
            # Never break a collection over provenance, but don't hide a persistent failure
            # (locked DB, registry problem) either: without this, server-source provenance
            # would silently never appear.
            logger.warning(
                "Could not record server credential sources for %s",
                scrub_log(provider_id),
                exc_info=True,
            )

    @staticmethod
    async def _source_candidates(
        provider_id: object, account_id: object, identity_verification: bool
    ) -> list[dict[str, Any]]:
        if not isinstance(provider_id, str) or not isinstance(account_id, str):
            return []
        if identity_verification:
            # Pending bundles may sit in a slot other than ``default`` (Anthropic keys them
            # by source id); each carries its ``account_slot``.
            return await token_cache.get_pending_sources(provider_id)
        slot_candidates = await token_cache.get_source_candidates(provider_id, account_id)
        if account_id != "default":
            return slot_candidates
        # Sidecar ingest files bundles under the resolved identity, never under
        # ``default``. Without this sweep a default collector finds nothing,
        # silently degrades to a single unpinned ``collect()`` (no cross-source
        # failover, merged-cache reads only) — the 2026-10-02 Antigravity
        # incident, where a dead merged token was served while a live source
        # bundle sat unused under its identity slot. Identity-pending rows from
        # the sweep are dropped by the pending filter in the caller.
        # Two separate cache reads with no shared lock between them: a push
        # that lands in between appears in exactly one of the two sets (never
        # a duplicate pin), and the dedupe below only covers ids present in
        # both snapshots.
        swept = await token_cache.get_account_source_candidates(provider_id)
        # Defensive: the two lookups are slot-scoped today, but if some future
        # path files the same source_id under both the default slot and an
        # identity slot, failover must not attempt that bundle twice.
        seen = {candidate["source_id"] for candidate in slot_candidates}
        return slot_candidates + [
            candidate for candidate in swept if candidate["source_id"] not in seen
        ]

    def _ordered_candidates(
        self,
        provider_id: str,
        account_id: str,
        candidates: list[dict[str, Any]],
        *,
        include_resting: bool = False,
    ) -> list[dict[str, Any]]:
        """Drop disabled sources and order the rest for failover.

        A bundle whose access token is already dead cannot answer this poll, so
        it goes behind every live (or undatable) bundle even when the operator
        ranked it first: priority orders usable credentials, it must not hand
        the first attempt to a credential that is known dead and can only
        reject the call (#474). Freshness only splits candidates into
        live-then-expired; within each group the configured (priority,
        source_id) order still decides.

        Preferences are keyed by each candidate's own ``account_slot`` — where
        the bundle was filed (the resolved identity) — falling back to the
        requesting collector's ``account_id`` when that slot has no row for
        the source, so a preference set from e.g. the default account view
        still applies to candidates the sweep brought in.
        """

        def candidate_preference(candidate: dict[str, Any]) -> tuple[bool, int] | None:
            source_id = candidate["source_id"]
            slot = candidate.get("account_slot") or account_id
            keys = [(provider_id, slot)]
            if slot != account_id:
                keys.append((provider_id, account_id))
            for key in keys:
                row = self._credential_source_preferences.get(key, {})
                if source_id in row:
                    return row[source_id]
            return None

        def candidate_enabled(candidate: dict[str, Any]) -> bool:
            preference = candidate_preference(candidate)
            if preference is not None:
                return bool(preference[0])
            return bool(candidate.get("enabled", True))

        def candidate_priority(candidate: dict[str, Any]) -> int:
            preference = candidate_preference(candidate)
            if preference is not None:
                return int(preference[1])
            return int(candidate.get("priority", 0))

        def candidate_state(candidate: dict[str, Any]) -> tuple[str, datetime | None]:
            slot = candidate.get("account_slot") or account_id
            for key in ((provider_id, slot), (provider_id, account_id)):
                state = self._credential_source_state.get(key, {}).get(candidate["source_id"])
                if state is not None:
                    return state
            return ("healthy", None)

        ordered = [candidate for candidate in candidates if candidate_enabled(candidate)]
        ordered.sort(
            key=lambda candidate: (
                self._access_token_expired(candidate),
                candidate_priority(candidate),
                candidate["source_id"],
            )
        )
        # A source whose last attempt was rejected rests between retries (15 min doubling to
        # 6 h), so it stops costing an upstream call at the head of every cycle. When the rest
        # is over it is simply due: it is tried in its normal place, and a success brings it
        # back while another failure doubles the rest. (Demoting it instead would starve it —
        # failover stops at the first source that works.) The last credential standing is
        # never starved: if every candidate is resting, the best one is still probed.
        now = datetime.now(UTC)

        def resting(candidate: dict[str, Any]) -> bool:
            retry_at = candidate_state(candidate)[1]
            if retry_at is None:
                return False
            return (retry_at if retry_at.tzinfo else retry_at.replace(tzinfo=UTC)) > now

        if include_resting:
            return ordered  # a probe looks at every enabled source, rested or not
        awake = [candidate for candidate in ordered if not resting(candidate)]
        return awake or ordered[:1]

    async def _collect_with_source_failover(
        self,
        key: str,
        client: httpx.AsyncClient,
        health_updates: dict[str, str],
    ) -> list[dict[str, Any]]:
        smart = self.smart_collectors[key]
        collector = smart.collector
        provider_id = getattr(collector, "PROVIDER_ID", None)
        account_id = (
            getattr(collector, "credential_account_id", None)
            or getattr(collector, "account_id", None)
            or "default"
        )
        default_account_label = getattr(collector, "account_label", None)
        identity_verification = is_verifier_key(key)
        candidates: list[dict[str, Any]] = []
        if isinstance(provider_id, str):
            candidates = await self._source_candidates(
                provider_id, account_id, identity_verification
            )
            # A regular default collector must not race the verifier for pending
            # sources. The dedicated collector sees only pending sidecar bundles.
            candidates = [
                candidate
                for candidate in candidates
                if bool(candidate.get("identity_pending")) == identity_verification
            ]
        if not candidates or not isinstance(provider_id, str):
            if identity_verification:
                return []
            return await asyncio.wait_for(smart.collect(client), timeout=25.0)

        # Ownership is judged against every source of the account (a pasted config
        # bundle can hold a machine's refresh secret), including ones filtered out below.
        all_candidates = list(candidates)
        candidates = await self._due_for_verification(
            identity_verification,
            provider_id,
            self._ordered_candidates(provider_id, account_id, candidates),
        )
        if not candidates:
            return []

        successful_result: list[dict[str, Any]] | None = None
        last_kept_failure_result: list[dict[str, Any]] | None = None
        skipped_for_renewal = False
        attempted: list[dict[str, Any]] = []
        deadline = asyncio.get_running_loop().time() + 25.0
        await smart.reset()
        for index, candidate in enumerate(candidates):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            if index:
                await smart.reset()
            self._reset_attempt_identity(collector, account_id, default_account_label)
            if self._awaiting_machine_renewal(provider_id, candidate, all_candidates):
                # An idle CLI let its access token lapse. The machine renews it (the server
                # must not: rotating its refresh token would sign that CLI out), so calling
                # the API now could only 401 and flag a healthy login as revoked. No health
                # update: the row keeps its last real outcome and reads "expired".
                logger.info(
                    "Skipping %s/%s source %s: expired, waiting for its machine to renew it",
                    scrub_log(provider_id),
                    scrub_log(account_id),
                    scrub_log(candidate["source_id"]),
                )
                skipped_for_renewal = True
                continue
            cache_slot = candidate.get("account_slot") or account_id
            attempted.append(candidate)
            async with token_cache.using_source(
                provider_id, cache_slot, candidate["source_id"]
            ) as attempt:
                result: list[dict[str, Any]] = []
                try:
                    result = await asyncio.wait_for(smart.collect(client), timeout=remaining)
                except Exception:
                    logger.exception(
                        "Credential source collection failed for %s/%s (%s)",
                        scrub_log(provider_id),
                        scrub_log(account_id),
                        scrub_log(candidate["source_id"]),
                    )
                    health_updates[candidate["source_id"]] = "unavailable"
                    # Do not retain a result from a crashed collector; returned
                    # failure cards are handled below, after collection completes.
                    continue
            # A failed fetch may still carry cached or partial usable cards; fail over only
            # when the response has no usable card at all (the on-demand source probe
            # classifies with the same rule).
            outcome = source_outcome(
                result,
                bool(attempt["auth_failed"]),
                bool(getattr(collector, "successful_empty_result", False)),
            )
            # Keep the last useful failure card if a later source fails silently.
            if outcome == "auth_failed":
                health_updates[candidate["source_id"]] = "auth_failed"
                last_kept_failure_result = result or last_kept_failure_result
                continue
            if outcome == "degraded":
                # An optional request or a refresh retry may return 401/403 even
                # though the collector produced usable quota. Keep the data;
                # preserve the partial failure for the source diagnostics.
                health_updates[candidate["source_id"]] = "degraded"
            if outcome == "unavailable":
                # Any failed attempt that did not trigger the auth_failed short-circuit
                # (e.g. missing_config, api_error, or empty response) is functionally down.
                # Note: Transient errors (rate_limited, timeout) trigger failover to try the
                # next candidate; rate-limit backoff is managed upstream by SmartCollector._mark_429.
                # The 'unavailable' status persists across polls until overwritten by the next
                # successful collection pass.
                health_updates[candidate["source_id"]] = "unavailable"
                last_kept_failure_result = result or last_kept_failure_result
                continue
            if not identity_verification and await self._cookie_switched_account(
                provider_id, account_id, candidate, collector
            ):
                # The browser now holds a different account than the one this source is tagged
                # to: the tag is gone and the source is pending again. Publish nothing from
                # this poll under the old account.
                continue
            if outcome == "healthy":
                health_updates[candidate["source_id"]] = "healthy"
            # A provider may learn a stable identity only after calling its
            # upstream API (Antigravity userinfo is one example). Bind that
            # identity to the exact source used for this successful attempt.
            # Never infer it from another account's historical usage.
            resolved_id = getattr(collector, "account_id", None)
            if (
                account_id == "default"
                and is_sidecar_source(candidate)
                and candidate.get("credential_origin")
                and isinstance(resolved_id, str)
                and resolved_id.strip()
                and resolved_id.strip().lower() != "default"
            ):
                await self._promote_source_identity(
                    provider_id,
                    account_id,
                    candidate["source_id"],
                    resolved_id,
                    cache_account_id=cache_slot,
                )
            elif (
                account_id == "default"
                and is_sidecar_source(candidate)
                and candidate.get("credential_origin")
            ):
                # Let an unresolved source call its API so it can prove its
                # identity, but never publish an unidentified quota card into
                # the shared default account history.
                from app.services.credential_tags import safe_quota_preview

                preview = safe_quota_preview(
                    [card for card in result if not card.get("error_type")]
                )
                await asyncio.to_thread(
                    self._persist_identity_pending_preview, provider_id, candidate, preview
                )
                # Keep going: a source that works but cannot name its account must not
                # starve the other pending sources of their own verification.
                successful_result = []
                continue
            successful_result = result
            break
        # Every source tried but not promoted (a promotion deletes its pending row, so
        # noting it is a no-op) is scheduled for a later retry.
        await self._note_unverified(identity_verification, provider_id, attempted)
        if successful_result is None and last_kept_failure_result is None and skipped_for_renewal:
            # Nothing ran, so the collector still holds the previous poll's state (often
            # "complete"). Say "skipped" so the poller keeps the last good cards instead of
            # reconciling them away, and the server-credential stamping leaves rows alone.
            smart._set_collection_state("skipped", "waiting for a machine to renew its login")
        return (
            successful_result if successful_result is not None else (last_kept_failure_result or [])
        )

    @staticmethod
    def _reset_attempt_identity(collector: Any, account_id: str, default_label: Any) -> None:
        """Clear what a previous source attempt taught the shared collector instance."""
        if account_id == "default":
            # Collectors can mutate account_id after an API response. Reset before each source
            # attempt so a verified identity from source A can never be attributed to source B
            # on a later poll.
            collector.account_id = "default"
            collector.account_label = default_label
        if hasattr(collector, "credential_account_id"):
            collector.credential_account_id = account_id
        # Proof from one source's response must never be read as another's.
        collector.verified_identity = None
        collector.verified_subject = None

    async def _cookie_switched_account(
        self,
        provider_id: str,
        account_id: str,
        candidate: dict[str, Any],
        collector: Any,
    ) -> bool:
        """Detect a browser account switch behind a machine-scoped cookie tag, and undo the tag.

        A cookie's origin is the same string whichever account the browser is signed into, so a
        tag keeps applying after a switch. Collectors that learn the account's email from the
        provider (``verified_identity``) let the server notice: when that email resolves to a
        *different canonical account* than the (email-shaped) tagged one, the tag is deleted,
        the source leaves the old account, and the next sidecar push files it as pending so the
        identity verifier maps it to the new account. Anything uncertain is left alone: a tag on
        a non-email account (hash-keyed, label-based, ``user@x @ Org``) is never second-guessed.
        Accounts are keyed by login email, so an operator's own tag on a *different* email than
        the one the cookie logs into counts as a switch too (the verifier then re-maps it).
        """
        from app.services.account_identity import EMAIL_RE, canonical_account_id
        from app.services.credential_sources import is_machine_bound_origin

        origin = candidate.get("credential_origin")
        sidecar_id = candidate.get("sidecar_id")
        verified = getattr(collector, "verified_identity", None)
        subject = getattr(collector, "verified_subject", None)
        if (
            candidate.get("identity_pending")
            or not is_sidecar_source(candidate)
            or not isinstance(origin, str)
            or not origin.startswith("cookie:")
            or not is_machine_bound_origin(origin)
            or not isinstance(sidecar_id, str)
        ):
            return False
        if isinstance(verified, str) and EMAIL_RE.match(verified):
            switched = EMAIL_RE.match(account_id) is not None and canonical_account_id(
                verified
            ) != canonical_account_id(account_id)
        else:
            # No email from the provider: fall back to its stable subject, if it gave one.
            switched = isinstance(subject, str) and await asyncio.to_thread(
                self._subject_drifted, provider_id, account_id, candidate["source_id"], subject
            )
        if not switched:
            return False
        revoked = await asyncio.to_thread(
            self._revoke_cookie_tag,
            provider_id,
            account_id,
            candidate["source_id"],
            origin,
            sidecar_id,
        )
        if not revoked:
            logger.debug(
                "Cookie switch behind %s/%s left alone: no sidecar-scoped operator tag to drop",
                scrub_log(provider_id),
                scrub_log(account_id),
            )
            return False
        await token_cache.remove_source(
            provider_id, account_id, candidate["source_id"], retire_matching_oauth=True
        )
        logger.warning(
            "A browser on %s switched account behind %s: dropped its %s cookie tag; "
            "the source is pending again",
            scrub_log(sidecar_id),
            scrub_log(account_id),
            scrub_log(provider_id),
        )
        return True

    @staticmethod
    def _subject_drifted(provider_id: str, account_id: str, source_id: str, subject: str) -> bool:
        """Compare the login behind a cookie with the one last seen for this source and account.

        The first sighting (or the first one after the source moved to another account, i.e. a
        re-tag) is recorded rather than compared; a different subject for the same account
        means the browser switched users.
        """
        from sqlmodel import Session, col, select

        from app.core.db import engine
        from app.models.db import CredentialSource
        from app.services.account_identity import canonical_account_id

        account = canonical_account_id(account_id)
        with Session(engine) as session:
            row = session.exec(
                select(CredentialSource).where(
                    CredentialSource.provider_id == provider_id,
                    CredentialSource.source_id == source_id,
                    col(CredentialSource.account_id).in_({account_id, account}),
                )
            ).first()
            if row is None:
                return False
            if row.verified_subject is None or row.verified_subject_account != account:
                row.verified_subject = subject
                row.verified_subject_account = account
                session.add(row)
                session.commit()
                return False
            return row.verified_subject.casefold() != subject.casefold()

    @staticmethod
    def _revoke_cookie_tag(
        provider_id: str, account_id: str, source_id: str, origin: str, sidecar_id: str
    ) -> bool:
        """Delete this sidecar's tag on a cookie origin and put its durable row back to pending.

        Only a sidecar-scoped tag that a person or a verification (not a mere claim) wrote
        whose account is the one being polled; an older deployment-wide tag is left to the
        operator because it applies on other machines too.
        """
        from sqlmodel import Session, col, select

        from app.core.db import engine
        from app.models.db import CredentialSource, CredentialTag
        from app.services.account_identity import canonical_account_id
        from app.services.credential_sources import pending_cache_slot
        from app.services.credential_tags import CredentialTagRepo

        with Session(engine) as session:
            tag = session.exec(
                select(CredentialTag).where(
                    CredentialTag.provider_id == provider_id,
                    CredentialTag.credential_origin == origin,
                    CredentialTag.sidecar_id == sidecar_id,
                )
            ).first()
            if (
                tag is None
                or tag.set_by == "identity_claim"
                or canonical_account_id(tag.account_id) != canonical_account_id(account_id)
            ):
                return False
            CredentialTagRepo.delete_tag(
                session, provider_id=provider_id, credential_origin=origin, sidecar_id=sidecar_id
            )
            pending_account = pending_cache_slot(provider_id, source_id)
            row = session.exec(
                select(CredentialSource).where(
                    CredentialSource.provider_id == provider_id,
                    col(CredentialSource.account_id).in_({account_id, tag.account_id}),
                    CredentialSource.source_id == source_id,
                )
            ).first()
            if row is not None:
                row.account_id = pending_account
                # Whoever logs in next is a new login, not a drift from the old one.
                row.verified_subject = None
                row.verified_subject_account = None
                session.add(row)
            session.commit()
        return True

    @staticmethod
    def _awaiting_machine_renewal(
        provider_id: str, candidate: dict[str, Any], all_candidates: list[dict[str, Any]]
    ) -> bool:
        """An expired rotating-provider login that a machine's CLI owns and will renew."""
        tokens = candidate.get("tokens") or {}
        if not has_refresh_credential(tokens):
            return False  # nothing will renew it: let the real call report it dead
        if not machine_owns_credential(
            provider_id, tokens, all_candidates, merged_source=candidate.get("sidecar_id")
        ):
            return False
        exp = IdentityExtractor.exp_from_tokens(tokens)
        return exp is not None and exp <= time.time()

    @staticmethod
    def _access_token_expired(candidate: dict[str, Any]) -> bool:
        """Whether this bundle's access token is already past its expiry.

        Dates the bearer the collector is about to send, via the shared
        ``exp_from_tokens`` resolver (access JWT ``exp`` first, then a stored
        ``expiry_date``) — the same clock ``_awaiting_machine_renewal`` trusts.
        A bundle with nothing parseable counts as live: an opaque token may be
        perfectly good, and demoting it on a guess would reorder credentials on
        no evidence.
        """
        exp = IdentityExtractor.exp_from_tokens(candidate.get("tokens") or {})
        return exp is not None and exp <= time.time()

    async def _promote_source_identity(
        self,
        provider_id: str,
        old_account_id: str,
        source_id: str,
        account_id: str,
        *,
        cache_account_id: str | None = None,
    ) -> None:
        """Move one default source to the stable identity it proved itself.

        ``old_account_id`` is the durable row's account (``default``); ``cache_account_id`` is
        the token-cache slot holding the bundle when that differs (Anthropic's pending
        bundles sit under their source id).
        """
        from datetime import UTC, datetime

        from sqlmodel import Session, col, select

        from app.core.db import engine
        from app.models.db import CredentialSource
        from app.services.account_identity import canonical_account_id
        from app.services.credential_tags import CredentialTagRepo, PendingCredentialTagRepo

        target = canonical_account_id(account_id)
        if not target or target == "default":
            return
        with Session(engine) as session:
            # The durable row sits under the account the bundle was filed under: ``default``,
            # or (Anthropic) the source id itself while the identity is pending.
            filed_under = {old_account_id}
            if cache_account_id:
                filed_under.add(cache_account_id)
            source = session.exec(
                select(CredentialSource).where(
                    CredentialSource.provider_id == provider_id,
                    col(CredentialSource.account_id).in_(filed_under),
                    CredentialSource.source_id == source_id,
                )
            ).first()
            if source is None or not source.credential_origin:
                return
            existing_tag = (
                CredentialTagRepo.get(
                    session,
                    provider_id=provider_id,
                    credential_origin=source.credential_origin,
                    sidecar_id=source.sidecar_id,
                )
                if source.sidecar_id
                else None
            )
            if existing_tag is not None and existing_tag.set_by not in (
                None,
                "identity_claim",
                "identity_verification",
                # Carried over from the previous fingerprint (#474): inferred,
                # not mapped for this origin, so a proved identity may correct
                # it — unlike the operator's own assignment below.
                "rotation",
            ):
                # An operator mapped this source while verification was in flight: theirs wins.
                logger.info(
                    "Not promoting %s: the operator already mapped this source",
                    scrub_log(source_id),
                )
                return
            existing_target = session.exec(
                select(CredentialSource).where(
                    CredentialSource.provider_id == provider_id,
                    CredentialSource.account_id == target,
                    CredentialSource.source_id == source_id,
                )
            ).first()
            source_sidecar_id = source.sidecar_id
            source_credential_origin = source.credential_origin
            if existing_target is None:
                source.account_id = target
                session.add(source)
            else:
                existing_target.source_type = source.source_type
                existing_target.source_label = source.source_label
                existing_target.credential_origin = source.credential_origin
                existing_target.sidecar_id = source.sidecar_id
                seen_values = [
                    value
                    for value in (source.last_seen, existing_target.last_seen)
                    if value is not None
                ]
                existing_target.last_seen = (
                    max(
                        value.replace(tzinfo=UTC) if value.tzinfo is None else value
                        for value in seen_values
                    )
                    if seen_values
                    else datetime.now(UTC)
                )
                session.add(existing_target)
                session.delete(source)
            if source_sidecar_id:
                CredentialTagRepo.set_tag(
                    session,
                    provider_id=provider_id,
                    credential_origin=source_credential_origin,
                    account_id=target,
                    sidecar_id=source_sidecar_id,
                    set_by="identity_verification",
                )
                PendingCredentialTagRepo.delete(
                    session,
                    sidecar_id=source_sidecar_id,
                    provider_id=provider_id,
                    credential_origin=source_credential_origin,
                )
            session.commit()
        moved = await self.reconcile_token_cache_from_durable_tags(
            provider_id=provider_id, source_id=source_id
        )
        if not moved:
            # Safety net if reconciliation cannot see the just-committed tag.
            logger.warning(
                "Credential source reconciliation did not move the verified source; "
                "using direct cache fallback (provider=%s, source_id=%s)",
                provider_id,
                source_id,
            )
            await token_cache.move_source(
                provider_id, cache_account_id or old_account_id, target, source_id
            )

    @classmethod
    async def _due_for_verification(
        cls, identity_verification: bool, provider_id: str, candidates: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if not identity_verification:
            return candidates
        return await asyncio.to_thread(cls._due_verification_candidates, provider_id, candidates)

    @classmethod
    async def _note_unverified(
        cls, identity_verification: bool, provider_id: str, attempted: list[dict[str, Any]]
    ) -> None:
        if identity_verification and attempted:
            await asyncio.to_thread(cls._note_verification_tries, provider_id, attempted)

    @staticmethod
    def _due_verification_candidates(
        provider_id: str, candidates: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Pending sources whose backoff has elapsed, oldest-due first, capped per cycle."""
        from datetime import UTC, datetime

        from sqlmodel import Session

        from app.core.db import engine
        from app.services.credential_tags import PendingCredentialTagRepo

        with Session(engine) as session:
            schedule = PendingCredentialTagRepo.get_verify_schedule(
                session, provider_id=provider_id
            )
        now = datetime.now(UTC)
        floor = datetime.min.replace(tzinfo=UTC)
        due: list[tuple[datetime, dict[str, Any]]] = []
        for candidate in candidates:
            key = (
                candidate.get("sidecar_id") or LOCAL_SIDECAR_ID,
                candidate.get("credential_origin"),
            )
            at = schedule.get(key)  # type: ignore[arg-type]
            if at is not None and at > now:
                continue
            due.append((at or floor, candidate))
        due.sort(key=lambda item: item[0])
        return [candidate for _, candidate in due[:MAX_VERIFICATIONS_PER_CYCLE]]

    @staticmethod
    def _note_verification_tries(provider_id: str, attempted: list[dict[str, Any]]) -> None:
        """Tries that left a source pending: schedule each one's next attempt."""
        from sqlmodel import Session

        from app.core.db import engine
        from app.services.credential_tags import PendingCredentialTagRepo

        for candidate in attempted:
            sidecar_id = candidate.get("sidecar_id") or LOCAL_SIDECAR_ID
            origin = candidate.get("credential_origin")
            if not isinstance(sidecar_id, str) or not isinstance(origin, str):
                continue
            # One session per source: a failing write loses that source's backoff only.
            try:
                with Session(engine) as session:
                    PendingCredentialTagRepo.record_verify_attempt(
                        session,
                        sidecar_id=sidecar_id,
                        provider_id=provider_id,
                        credential_origin=origin,
                    )
                    session.commit()
            except Exception:
                logger.exception(
                    "Could not schedule the next identity verification for %s source %s",
                    scrub_log(provider_id),
                    scrub_log(candidate.get("source_id")),
                )

    def _persist_identity_pending_preview(
        self,
        provider_id: str,
        candidate: dict[str, Any],
        preview: list[dict[str, Any]],
    ) -> None:
        """Persist safe quota fields against the pending credential row."""
        sidecar_id = candidate.get("sidecar_id")
        credential_origin = candidate.get("credential_origin")
        if not isinstance(sidecar_id, str) or not isinstance(credential_origin, str):
            return

        from sqlmodel import Session

        from app.core.db import engine
        from app.services.credential_tags import PendingCredentialTagRepo

        with Session(engine) as session:
            PendingCredentialTagRepo.set_quota_preview(
                session,
                sidecar_id=sidecar_id,
                provider_id=provider_id,
                credential_origin=credential_origin,
                preview=preview,
            )
            session.commit()

    async def reconcile_token_cache_from_durable_tags(
        self,
        *,
        provider_id: str | None = None,
        source_id: str | None = None,
    ) -> int:
        """Move cached sidecar credentials to accounts named by durable tags.

        Tags commit before the in-memory cache can be updated. Rechecking the
        durable mapping during startup/sync and after promotions closes that
        gap without relying on a later sidecar heartbeat.
        """
        from sqlmodel import Session, select

        from app.core.db import engine
        from app.models.db import CredentialSource
        from app.services.credential_tags import CredentialTagRepo

        provider_ids = [provider_id] if provider_id else list(self.collector_registry)
        moves: list[tuple[str, str, str, str, tuple[bool, int] | None]] = []
        candidates_by_provider: dict[str, list[dict[str, Any]]] = {}
        for pid in provider_ids:
            candidates_by_provider[pid] = await token_cache.get_all_source_descriptors(pid)
        with Session(engine) as session:
            for pid in provider_ids:
                candidates = candidates_by_provider[pid]
                for candidate in candidates:
                    candidate_id = candidate.get("source_id")
                    origin = candidate.get("credential_origin")
                    sidecar_id = candidate.get("sidecar_id")
                    if (
                        not is_sidecar_source(candidate)
                        or not isinstance(candidate_id, str)
                        or (source_id is not None and candidate_id != source_id)
                        or not isinstance(origin, str)
                        or not isinstance(sidecar_id, str)
                    ):
                        continue
                    target = CredentialTagRepo.get_account_id(
                        session,
                        provider_id=pid,
                        credential_origin=origin,
                        sidecar_id=sidecar_id,
                    )
                    if not target or target == "default":
                        continue
                    old_account_id = str(candidate.get("account_id") or "default")
                    source = session.exec(
                        select(CredentialSource).where(
                            CredentialSource.provider_id == pid,
                            CredentialSource.account_id == old_account_id,
                            CredentialSource.source_id == candidate_id,
                        )
                    ).first()
                    existing_target = session.exec(
                        select(CredentialSource).where(
                            CredentialSource.provider_id == pid,
                            CredentialSource.account_id == target,
                            CredentialSource.source_id == candidate_id,
                        )
                    ).first()
                    preference: tuple[bool, int] | None = None
                    if source is not None and old_account_id != target:
                        if existing_target is None:
                            preference = (bool(source.enabled), int(source.priority))
                            source.account_id = target
                            session.add(source)
                        else:
                            # Preserve a target row if legacy data or a future
                            # source-id re-key leaves both rows for one source.
                            preference = (
                                bool(existing_target.enabled),
                                int(existing_target.priority),
                            )
                            existing_target.credential_origin = source.credential_origin
                            existing_target.sidecar_id = source.sidecar_id
                            existing_target.source_type = source.source_type
                            existing_target.source_label = source.source_label
                            session.add(existing_target)
                            session.delete(source)
                    else:
                        preference = (
                            (bool(source.enabled), int(source.priority))
                            if source
                            else (bool(existing_target.enabled), int(existing_target.priority))
                            if existing_target
                            else None
                        )
                    moves.append(
                        (
                            pid,
                            candidate_id,
                            target,
                            old_account_id,
                            preference,
                        )
                    )
            # Durable tags are authoritative; a failed cache move is retried
            # from this mapping by the next startup or reconciliation pass.
            session.commit()

        for pid, candidate_id, target, old_account_id, preference in moves:
            await token_cache.move_source(pid, old_account_id, target, candidate_id)
            old_preferences = self._credential_source_preferences.get((pid, old_account_id))
            if old_preferences is not None:
                old_preferences.pop(candidate_id, None)
            if preference is not None:
                self._credential_source_preferences.setdefault((pid, target), {})[candidate_id] = (
                    preference
                )
        return len(moves)

    def _record_source_health(
        self, provider_id: str, account_id: str, updates: dict[str, str]
    ) -> None:
        if not updates:
            return

        from sqlmodel import Session, col
        from sqlmodel import select as sqlselect

        from app.core.db import engine
        from app.models.db import CredentialSource
        from app.services.credential_sources import record_source_result

        with Session(engine) as session:
            rows = list(
                session.exec(
                    sqlselect(CredentialSource).where(
                        CredentialSource.provider_id == provider_id,
                        CredentialSource.account_id == account_id,
                        col(CredentialSource.source_id).in_(updates),
                    )
                ).all()
            )
            # ``account_id`` is what the collector was keyed on *before* it ran. A
            # successful identity-pending verification promotes its source to the
            # resolved account mid-attempt (``_promote_source_identity``), so the row
            # no longer lives under the pre-run id. A source_id names one credential,
            # so fall back to wherever it is now rather than dropping the update.
            missing = set(updates) - {row.source_id for row in rows}
            if missing:
                rows.extend(
                    session.exec(
                        sqlselect(CredentialSource).where(
                            CredentialSource.provider_id == provider_id,
                            col(CredentialSource.source_id).in_(missing),
                        )
                    ).all()
                )
            fresh: list[tuple[str, str, str, datetime | None]] = []
            for row in rows:
                record_source_result(row, updates[row.source_id])
                session.add(row)
                fresh.append((row.account_id, row.source_id, row.health, row.next_retry_at))
            session.commit()
        # Failover reads this cache; waiting for the next sync would re-try a source that
        # just failed for up to a minute. Keyed by the row's own account (a promoted source no
        # longer lives under the collector's pre-run id), the same key the sync loads.
        for row_account_id, source_id, health, retry_at in fresh:
            self._credential_source_state.setdefault((provider_id, row_account_id), {})[
                source_id
            ] = (health, retry_at)

    def clear_source_retry(self, provider_id: str, source_id: str) -> None:
        """A re-login ended this source's rest: forget it in the failover cache too."""
        for (provider, _account), states in self._credential_source_state.items():
            if provider == provider_id and source_id in states:
                states[source_id] = (states[source_id][0], None)

    def get_collector_stats(self) -> dict[str, Any]:
        """Get flattened statistics for all active collectors."""
        return {"collectors": [sc.get_stats() for sc in self.smart_collectors.values()]}

    async def collect_one(
        self, provider_id: str, account_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Reset and immediately re-collect a single provider."""
        target_prefix = f"{provider_id}:"
        results: list[dict[str, Any]] = []
        client = await self._get_client()

        for key, sc in self.smart_collectors.items():
            if key.startswith(target_prefix):
                if (
                    account_id is None
                    or key == f"{provider_id}:{account_id}"
                    or (account_id == "default" and key == verifier_key(provider_id))
                ):
                    await sc.reset()
                    try:
                        res = await self._collect_with_semaphore(key, client)
                        if isinstance(res, list):
                            results.extend(res)
                    except Exception as e:
                        logger.error(f"Error collecting {key}: {e}")

        return results

    def _create_collector(self, provider_id: str) -> Any:
        """Instantiate a one-off collector for *provider_id* (not added to smart_collectors).

        Used by the debug endpoint to run a collector that is not currently active.
        Returns None if the provider is not registered.
        """
        entry = self.collector_registry.get(provider_id)
        if entry is None:
            return None
        cls, _name, _ttl = entry
        return cls()

    def set_credential_source_preferences(
        self, provider_id: str, account_id: str, preferences: dict[str, tuple[bool, int]]
    ) -> None:
        """Replace the in-memory source preferences for one account."""
        self._credential_source_preferences[(provider_id, account_id)] = preferences

    def set_credential_source_preference(
        self, provider_id: str, account_id: str, source_id: str, enabled: bool, priority: int
    ) -> None:
        """Set one source's in-memory (enabled, priority) without touching its siblings."""
        self._credential_source_preferences.setdefault((provider_id, account_id), {})[source_id] = (
            enabled,
            priority,
        )

    def drop_credential_source_preference(
        self, provider_id: str, account_id: str, source_id: str
    ) -> None:
        """Forget one source's in-memory preference (its row is gone)."""
        preferences = self._credential_source_preferences.get((provider_id, account_id))
        if preferences is not None:
            preferences.pop(source_id, None)

    def clear_credential_source_preferences(self, provider_id: str, account_id: str) -> None:
        """Drop the in-memory source preferences for one account."""
        self._credential_source_preferences.pop((provider_id, account_id), None)

    async def reset_collector(self, provider_id: str, account_id: str | None = None):
        """Reset internal state for specific collector(s)."""
        target_prefix = f"{provider_id}:"

        for key, sc in self.smart_collectors.items():
            if key.startswith(target_prefix):
                if account_id is None or key == f"{provider_id}:{account_id}":
                    await sc.reset()

    def get_supported_strategies(self, provider_id: str) -> list[dict]:
        """
        Return the list of supported strategies for a given provider.
        Each entry: {"id": str, "label": str}
        Returns [] if the provider has no declared STRATEGIES.
        """
        entry = self.collector_registry.get(provider_id)
        if entry is None:
            return []
        cls, _name, _ttl = entry
        strategies = getattr(cls, "STRATEGIES", {})
        result = []
        for s_id, entry in strategies.items():
            if len(entry) >= 2:
                label = entry[0]
                result.append({"id": s_id, "label": label})
        return result


# Global instance
collector_manager = CollectorManager()
manager = collector_manager
