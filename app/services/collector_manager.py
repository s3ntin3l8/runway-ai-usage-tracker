"""
Manages collection of AI provider quotas with smart differential fetching.

This module orchestrates all collectors and wraps them with SmartCollector
for intelligent caching to reduce API calls while maintaining fresh data.
Now supports multi-account dynamic spawning based on discovered tokens.
"""

import asyncio
import logging
import os
import platform
import time
from typing import Any

import httpx

from app.core.config import is_local_credential_scraping_enabled
from app.services.collectors.anthropic import AnthropicCollector
from app.services.collectors.antigravity import AntigravityCollector
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
from app.services.external_metrics import external_metric_service
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
            "anthropic": (AnthropicCollector, "Claude (Anthropic)", 60),
            "gemini": (GeminiCollector, "Gemini", 300),
            "github": (GitHubCollector, "GitHub Copilot", 900),
            "chatgpt": (ChatGPTCollector, "ChatGPT", 600),
            "antigravity": (AntigravityCollector, "Antigravity", 900),
            "opencode": (OpenCodeCollector, "OpenCode", 1800),
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
        self._keychain_warmed_up = False
        self._last_sync_time: float = 0.0
        self._collect_lock = asyncio.Lock()
        self._collect_future: asyncio.Future | None = None
        # In-memory registry: latest snapshot of all cards (populated by poller + startup)
        self._registry: list[dict[str, Any]] = []

        logger.info(
            f"CollectorManager initialized with {len(self.collector_registry)} registered providers"
        )

    async def _warmup_keychain(self):
        """Sequentially pre-fetch keychain secrets on macOS."""
        if self._keychain_warmed_up:
            return
        if platform.system() != "Darwin" or not is_local_credential_scraping_enabled():
            self._keychain_warmed_up = True
            return

        from app.core.keychain import get_keychain_secret

        tasks = []

        if not os.getenv("CLAUDE_CODE_OAUTH_TOKEN"):
            tasks.append(asyncio.to_thread(get_keychain_secret, "Claude Code-credentials"))

        cookie_collectors = ["anthropic", "chatgpt", "opencode", "kimi", "ollama"]
        if any(os.getenv(f"{c.upper()}_SESSION_TOKEN") is None for c in cookie_collectors):
            tasks.append(asyncio.to_thread(get_keychain_secret, "Chrome Safe Storage"))
            tasks.append(asyncio.to_thread(get_keychain_secret, "Microsoft Edge Safe Storage"))

        if tasks:
            for task in tasks:
                try:
                    await task
                except Exception as e:
                    logger.debug(f"Keychain warmup task failed: {e}")

        self._keychain_warmed_up = True

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
            # Load DB provider configs (enabled flag + poll interval overrides)
            db_configs: dict[str, Any] = {}
            global_poll_interval: int | None = None
            try:
                from sqlmodel import Session
                from sqlmodel import select as sqlselect

                from app.core.db import engine
                from app.models.db import ProviderConfig, SystemConfig

                with Session(engine) as _s:
                    db_configs = {
                        r.provider_id: r for r in _s.exec(sqlselect(ProviderConfig)).all()
                    }
                    sys_cfg = _s.exec(sqlselect(SystemConfig)).first()
                    if sys_cfg and sys_cfg.default_poll_interval_seconds:
                        global_poll_interval = sys_cfg.default_poll_interval_seconds
            except Exception as e:
                logger.debug(f"Could not load provider configs from DB: {e}")

            # 1. Ensure Default/Static collectors are present
            for p_id, (cls, name, ttl) in self.collector_registry.items():
                db_cfg = db_configs.get(p_id)
                if db_cfg is not None and not db_cfg.enabled:
                    # Remove existing collector if it was previously running
                    self.smart_collectors.pop(f"{p_id}:default", None)
                    # Purge stale cards so the dashboard reflects the change immediately
                    self._registry = [c for c in self._registry if c.get("provider_id") != p_id]
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
                    self.smart_collectors[key] = SmartCollector(
                        collector=cls(account_label=db_label),
                        collector_name=name,
                        ttl=effective_ttl,
                    )
                else:
                    sc = self.smart_collectors[key]
                    if effective_ttl != sc.ttl:
                        sc.ttl = effective_ttl
                    # Propagate updated account_label from ProviderConfig
                    if db_label and sc.collector.account_label != db_label:
                        sc.collector.account_label = db_label

            # 2. Discover active dynamic collectors from TokenCache
            active_accounts = await token_cache.get_all_active_accounts()
            active_keys = set()
            for p_id, acc_id, acc_name in active_accounts:
                if p_id in self.collector_registry:
                    cls, name, ttl = self.collector_registry[p_id]
                    db_cfg = db_configs.get(p_id)
                    if db_cfg is not None and not db_cfg.enabled:
                        # Remove existing dynamic collector and purge its stale cards
                        self.smart_collectors.pop(f"{p_id}:{acc_id}", None)
                        self._registry = [
                            c
                            for c in self._registry
                            if not (c.get("provider_id") == p_id and c.get("account_id") == acc_id)
                        ]
                        continue
                    effective_ttl = (
                        db_cfg.poll_interval_seconds
                        if db_cfg and db_cfg.poll_interval_seconds
                        else global_poll_interval or ttl
                    )
                    key = f"{p_id}:{acc_id}"
                    active_keys.add(key)
                    if key not in self.smart_collectors:
                        full_name = f"{name} ({acc_name or acc_id[:6]})"
                        logger.info(f"Spawning dynamic collector for {p_id} account {acc_id}")
                        self.smart_collectors[key] = SmartCollector(
                            collector=cls(account_id=acc_id, account_label=acc_name),
                            collector_name=full_name,
                            ttl=effective_ttl,
                        )

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
        """Close the persistent httpx client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

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
                future.set_result(result)
            except Exception as e:
                future.set_exception(e)
                raise

        return await future

    async def _do_collect(self) -> list[dict[str, Any]]:
        """Execute one collection cycle across all active collectors."""
        # Ensure we have collectors for all current accounts
        await self._sync_collectors()

        # Warm up keychain access if on macOS
        await self._warmup_keychain()

        client = await self._get_client()

        active_keys = list(self.smart_collectors.keys())
        tasks = [
            asyncio.wait_for(self.smart_collectors[key].collect(client), timeout=25.0)
            for key in active_keys
        ]

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

        # Merge external metrics (Sidecars)
        external_results = await external_metric_service.get_all_metrics()
        flattened.extend(external_results)

        logger.info(
            f"Collected {len(flattened)} total cards from {len(active_keys)} active accounts"
        )
        # Update registry atomically inside _do_collect so all callers (startup,
        # poller, fallback) share one authoritative write path.
        self._registry = flattened
        return flattened

    def get_registry_snapshot(self) -> list[dict[str, Any]]:
        """Return current in-memory card registry. Never blocks."""
        return list(self._registry)  # shallow copy prevents external mutation

    def get_collector_stats(self) -> dict[str, Any]:
        """Get flattened statistics for all active collectors."""
        return {"collectors": [sc.get_stats() for sc in self.smart_collectors.values()]}

    async def collect_one(
        self, provider_id: str, account_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Reset and immediately re-collect a single provider, merging results into the registry."""
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

        # Merge: replace old cards for this provider (and account if specified)
        if account_id:
            kept = [
                c
                for c in self._registry
                if not (c.get("provider_id") == provider_id and c.get("account_id") == account_id)
            ]
        else:
            kept = [c for c in self._registry if c.get("provider_id") != provider_id]
        self._registry = kept + results
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


# Global instance
collector_manager = CollectorManager()
manager = collector_manager
