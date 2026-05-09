"""
Manages collection of AI provider quotas with smart differential fetching.

This module orchestrates all collectors and wraps them with SmartCollector
for intelligent caching to reduce API calls while maintaining fresh data.
Now supports multi-account dynamic spawning based on discovered tokens.
"""

import asyncio
import logging
import time
from typing import Any

import httpx

from app.services.collectors.anthropic import AnthropicCollector
from app.services.collectors.chatgpt import ChatGPTCollector
from app.services.collectors.gemini import GeminiCollector
from app.services.collectors.github import GitHubCollector
from app.services.collectors.kimi_api import KimiApiCollector
from app.services.collectors.kimi_coding import KimiCodingCollector
from app.services.collectors.kimi_k2 import KimiK2Collector
from app.services.collectors.minimax import MiniMaxCollector
from app.services.collectors.ollama import OllamaCollector
from app.services.collectors.opencode import OpenCodeCollector
from app.services.collectors.openrouter import OpenRouterCollector
from app.services.collectors.zai import ZaiCollector
from app.services.smart_collector import SmartCollector
from app.services.token_cache import token_cache

logger = logging.getLogger(__name__)


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
            "gemini": (GeminiCollector, "Gemini", 900),
            "github": (GitHubCollector, "GitHub Copilot", 900),
            "chatgpt": (ChatGPTCollector, "ChatGPT", 900),
            "opencode": (OpenCodeCollector, "OpenCode", 900),
            "zai": (ZaiCollector, "zAI", 900),
            "kimi_api": (KimiApiCollector, "Kimi API", 900),
            "kimi_coding": (KimiCodingCollector, "Kimi Coding", 900),
            "kimi_k2": (KimiK2Collector, "Kimi K2", 900),
            "openrouter": (OpenRouterCollector, "OpenRouter", 900),
            "minimax": (MiniMaxCollector, "MiniMax", 900),
            "ollama": (OllamaCollector, "Ollama Cloud", 900),
        }

        # Active collectors keyed by "provider_id:account_id"
        self.smart_collectors: dict[str, SmartCollector] = {}
        self._client = None
        self._last_sync_time: float = 0.0
        self._collect_lock = asyncio.Lock()
        self._collect_future: asyncio.Future | None = None
        # Concurrency limit: max 10 collectors running at once
        self._semaphore = asyncio.Semaphore(10)

        logger.info(
            f"CollectorManager initialized with {len(self.collector_registry)} registered providers"
        )

    async def _sync_collectors(self):
        """Synchronize active SmartCollectors with discovered accounts.

        Throttled to run at most once every 60 seconds to avoid redundant
        TokenCache lookups on every /api/limits request.
        """
        if time.time() - self._last_sync_time < 60.0:
            return
        async with self._sync_lock:
            # Re-check inside lock to avoid double-sync when multiple requests
            # are waiting on the lock simultaneously.
            if time.time() - self._last_sync_time < 60.0:
                return
            # Load DB provider configs: provider_id -> account_id -> ProviderConfig
            # We use a nested map to handle multi-account overrides correctly.
            db_configs: dict[str, dict[str, Any]] = {}
            global_poll_interval: int | None = None
            try:
                from sqlmodel import Session
                from sqlmodel import select as sqlselect

                from app.core.db import engine
                from app.models.db import ProviderConfig, SystemConfig

                with Session(engine) as _s:
                    for r in _s.exec(sqlselect(ProviderConfig)).all():
                        if r.provider_id not in db_configs:
                            db_configs[r.provider_id] = {}
                        db_configs[r.provider_id][r.account_id] = r

                        # Sync manual tokens to cache to survive reloads/restarts
                        if r.enabled and (r.api_key or r.session_cookie):
                            await self._sync_manual_config_to_cache(r)

                    sys_cfg = _s.exec(sqlselect(SystemConfig)).first()
                    if sys_cfg and sys_cfg.default_poll_interval_seconds:
                        global_poll_interval = sys_cfg.default_poll_interval_seconds
            except Exception as e:
                logger.debug(f"Could not load provider configs from DB: {e}")

            # 1. Ensure Default/Static collectors are present
            for p_id, (cls, name, ttl) in self.collector_registry.items():
                # For default collector, we look for account_id="default"
                provider_acc_configs = db_configs.get(p_id, {})
                db_cfg = provider_acc_configs.get("default")

                if db_cfg is not None and not db_cfg.enabled:
                    # Remove existing collector if it was previously running
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
            for p_id, acc_id, acc_name in active_accounts:
                if p_id in self.collector_registry:
                    default_key = f"{p_id}:default"
                    if default_key in self.smart_collectors:
                        logger.debug(
                            f"Skipping dynamic collector for {p_id}, default already running"
                        )
                        continue
                    cls, name, ttl = self.collector_registry[p_id]

                    # For dynamic account, check for specific override OR fallback to default provider override
                    provider_acc_configs = db_configs.get(p_id, {})
                    db_cfg = provider_acc_configs.get(acc_id) or provider_acc_configs.get("default")

                    if db_cfg is not None and not db_cfg.enabled:
                        # Remove existing dynamic collector and its stale cards
                        self.smart_collectors.pop(f"{p_id}:{acc_id}", None)
                        continue
                    effective_ttl = (
                        db_cfg.poll_interval_seconds
                        if db_cfg and db_cfg.poll_interval_seconds
                        else global_poll_interval or ttl
                    )

                    # Prioritize DB override label, then acc_name from cache
                    db_label = db_cfg.account_label if db_cfg else None
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
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def close(self):
        """Close the internal HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _sync_manual_config_to_cache(self, r):
        """Helper to push a single ProviderConfig into the token cache."""
        from app.core.utils import IdentityExtractor

        all_tokens = {}

        # Handle API Key (OAuth Token)
        if r.api_key:
            # Strip Bearer prefix if present
            token_val = r.api_key
            if token_val.lower().startswith("bearer "):
                token_val = token_val[7:].strip()

            all_tokens["oauth_token"] = token_val
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

        if all_tokens:
            await token_cache.store(
                r.provider_id,
                all_tokens,
                account_id=r.account_id or "default",
                source="config",
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
                future = asyncio.get_event_loop().create_future()
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
        tasks = [self._collect_with_semaphore(key, client) for key in active_keys]

        try:
            results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True), timeout=45.0
            )
        except TimeoutError:
            logger.error("Global collector timeout reached. Collection aborted.")
            results = []

        flattened = []
        for i, res in enumerate(results):
            if isinstance(res, Exception):
                logger.error(f"Unexpected error from collector {active_keys[i]}: {res}")
                continue
            if isinstance(res, list):
                flattened.extend(res)

        logger.info(
            f"Collected {len(flattened)} total cards from {len(active_keys)} active accounts"
        )
        return flattened

    async def _collect_with_semaphore(
        self, key: str, client: httpx.AsyncClient
    ) -> list[dict[str, Any]]:
        """Run a single collector with semaphore/timeout protection."""
        async with self._semaphore:
            return await asyncio.wait_for(self.smart_collectors[key].collect(client), timeout=25.0)

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
                if account_id is None or key == f"{provider_id}:{account_id}":
                    await sc.reset()
                    try:
                        res = await sc.collect(client)
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
