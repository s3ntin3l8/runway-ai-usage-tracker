"""
Manages collection of AI provider quotas with smart differential fetching.

This module orchestrates all collectors and wraps them with SmartCollector
for intelligent caching to reduce API calls while maintaining fresh data.
Now supports multi-account dynamic spawning based on discovered tokens.
"""

import asyncio
import httpx
import logging
import os
import platform
from typing import List, Dict, Any, Tuple

from app.core.config import settings
from app.services.token_cache import token_cache

from app.services.collectors.anthropic import AnthropicCollector
from app.services.collectors.gemini import GeminiCollector
from app.services.collectors.github import GitHubCollector
from app.services.collectors.chatgpt import ChatGPTCollector
from app.services.collectors.antigravity import AntigravityCollector
from app.services.collectors.opencode import OpenCodeCollector
from app.services.collectors.zai_api import ZaiApiCollector
from app.services.collectors.zai_plan import ZaiPlanCollector
from app.services.collectors.kimi_api import KimiApiCollector
from app.services.collectors.kimi_coding import KimiCodingCollector
from app.services.collectors.openrouter import OpenRouterCollector
from app.services.collectors.minimax import MiniMaxCollector
from app.services.collectors.ollama import OllamaCollector
from app.services.smart_collector import SmartCollector
from app.services.external_metrics import external_metric_service

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
        # Registry of available collector classes and their default settings
        self.collector_registry = {
            "anthropic": (AnthropicCollector, "Claude (Anthropic)", 60),
            "gemini": (GeminiCollector, "Gemini", 300),
            "github": (GitHubCollector, "GitHub Copilot", 900),
            "chatgpt": (ChatGPTCollector, "ChatGPT", 600),
            "antigravity": (AntigravityCollector, "Antigravity", 900),
            "opencode": (OpenCodeCollector, "OpenCode", 1800),
            "zai_api": (ZaiApiCollector, "zAI API", 900),
            "zai_plan": (ZaiPlanCollector, "zAI Plan", 900),
            "kimi_api": (KimiApiCollector, "Kimi API", 900),
            "kimi_coding": (KimiCodingCollector, "Kimi Coding", 900),
            "openrouter": (OpenRouterCollector, "OpenRouter", 900),
            "minimax": (MiniMaxCollector, "MiniMax", 900),
            "ollama": (OllamaCollector, "Ollama Cloud", 900),
        }

        # Active collectors keyed by "provider_id:account_id"
        self.smart_collectors: Dict[str, SmartCollector] = {}
        self._client = None
        self._keychain_warmed_up = False
        
        logger.info(
            f"CollectorManager initialized with {len(self.collector_registry)} registered providers"
        )

    async def _warmup_keychain(self):
        """Sequentially pre-fetch keychain secrets on macOS."""
        if self._keychain_warmed_up:
            return
        if platform.system() != "Darwin" or not settings.LOCAL_CREDENTIAL_SCRAPING_ENABLED:
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
        """Synchronize active SmartCollectors with discovered accounts."""
        # 1. Ensure Default/Static collectors are present
        for p_id, (cls, name, ttl) in self.collector_registry.items():
            key = f"{p_id}:default"
            if key not in self.smart_collectors:
                logger.info(f"Spawning default collector for {p_id}")
                self.smart_collectors[key] = SmartCollector(
                    collector=cls(),
                    collector_name=name,
                    ttl=ttl
                )

        # 2. Discover active dynamic collectors from TokenCache
        active_accounts = await token_cache.get_all_active_accounts()
        active_keys = set()
        for p_id, acc_id, acc_name in active_accounts:
            if p_id in self.collector_registry:
                cls, name, ttl = self.collector_registry[p_id]
                key = f"{p_id}:{acc_id}"
                active_keys.add(key)
                if key not in self.smart_collectors:
                    full_name = f"{name} ({acc_name or acc_id[:6]})"
                    logger.info(f"Spawning dynamic collector for {p_id} account {acc_id}")
                    self.smart_collectors[key] = SmartCollector(
                        collector=cls(account_id=acc_id, account_label=acc_name),
                        collector_name=full_name,
                        ttl=ttl
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

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create a persistent httpx client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def collect_all(self) -> List[Dict[str, Any]]:
        """
        Collect all limits across all active accounts.
        """
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
        except asyncio.TimeoutError:
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

        logger.info(f"Collected {len(flattened)} total cards from {len(active_keys)} active accounts")
        return flattened

    def get_collector_stats(self) -> Dict[str, Any]:
        """Get flattened statistics for all active collectors."""
        return {
            "collectors": [
                sc.get_stats() for sc in self.smart_collectors.values()
            ]
        }


# Global instance
collector_manager = CollectorManager()
