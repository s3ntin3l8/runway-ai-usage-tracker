"""
Manages collection of AI provider quotas with smart differential fetching.

This module orchestrates all collectors and wraps them with SmartCollector
for intelligent caching to reduce API calls while maintaining fresh data.
"""

import asyncio
import httpx
import logging
from typing import List, Dict, Any

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
from app.services.smart_collector import SmartCollector
from app.services.external_metrics import external_metric_service

logger = logging.getLogger(__name__)


class CollectorManager:
    """
    Manages collection of all AI provider quotas with smart differential fetching.
    
    Wraps each collector with SmartCollector to implement:
    - Per-collector TTL caching (5-30 minutes depending on provider)
    - Error tracking and automatic retry with backoff
    - Graceful degradation (show stale data vs errors)
    - Reduced API calls through differential fetching
    
    TTL Strategy:
    - Fast-changing providers (Gemini): 5 minutes
    - Medium providers (Anthropic, GitHub): 10-15 minutes
    - Slow-changing providers (OpenCode): 30 minutes
    """
    
    def __init__(self):
        """Initialize collector configurations for lazy loading."""
        # Define collectors with names and TTL values (classes instead of instances)
        self.collector_configs = [
            (AnthropicCollector, "Claude (Anthropic)", 600),      # 10 min
            (GeminiCollector, "Gemini", 300),                     # 5 min
            (GitHubCollector, "GitHub Copilot", 900),             # 15 min
            (ChatGPTCollector, "ChatGPT", 600),                   # 10 min
            (AntigravityCollector, "Antigravity", 900),           # 15 min
            (OpenCodeCollector, "OpenCode", 1800),                # 30 min
            (ZaiApiCollector, "zAI API", 900),                    # 15 min
            (ZaiPlanCollector, "zAI Plan", 900),                  # 15 min
            (KimiApiCollector, "Kimi API", 900),                  # 15 min
            (KimiCodingCollector, "Kimi Coding", 900)             # 15 min
        ]
        
        self.smart_collectors = []
        self._client = None
        logger.info(f"CollectorManager initialized with {len(self.collector_configs)} collector configs")

    def _lazy_load_collectors(self):
        """Instantiate collectors only when first needed."""
        if not self.smart_collectors:
            self.smart_collectors = [
                SmartCollector(
                    collector=collector_cls(),
                    collector_name=name,
                    ttl=ttl,
                    error_threshold=3,      # Force retry after 3 consecutive errors
                    error_retry_delay=30.0  # Wait 30s before retrying after error
                )
                for collector_cls, name, ttl in self.collector_configs
            ]
            logger.info(f"Lazy loaded {len(self.smart_collectors)} collectors")

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create a persistent httpx client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client
    
    async def collect_all(self) -> List[Dict[str, Any]]:
        """
        Collect all limits using smart differential fetching.
        
        Process:
        1. Fetch from all SmartCollectors concurrently (with timeout)
        2. Each SmartCollector decides:
           - Return cached data if fresh
           - Fetch fresh data if stale
           - Return stale data if fetch fails (graceful degradation)
        3. Flatten results and merge external metrics
        
        Returns:
            List[Dict[str, Any]]: All limit cards from all sources
        """
        self._lazy_load_collectors()
        client = await self._get_client()
        try:
            # Run all collectors concurrently with exception handling and per-collector timeouts
            # Each collector gets 10s to complete, with a 25s global timeout as safety
            tasks = [
                asyncio.wait_for(smart_collector.collect(client), timeout=10.0)
                for smart_collector in self.smart_collectors
            ]
            # Wrap with global timeout to protect against I/O hangs
            results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=25.0
            )
        except asyncio.TimeoutError:
            logger.error("Global collector timeout reached. Collection aborted.")
            results = []
        
        # Flatten results, handling exceptions
        flattened = []
        for i, res in enumerate(results):
            if isinstance(res, Exception):
                # SmartCollector handles exceptions, so this shouldn't happen
                # But log it just in case
                smart_collector = self.smart_collectors[i]
                logger.error(
                    f"Unexpected exception from {smart_collector.collector_name}: {res}"
                )
                continue
            
            if isinstance(res, list):
                flattened.extend(res)
        
        # Merge external metrics
        external_results = external_metric_service.get_all_metrics()
        flattened.extend(external_results)
        
        logger.info(f"Collected {len(flattened)} total limit cards from all sources")
        return flattened
    
    def get_collector_stats(self) -> Dict[str, Any]:
        """
        Get statistics about collector cache states and error tracking.
        
        Useful for monitoring dashboard or debugging.
        
        Returns:
            Dictionary with stats for each collector
        """
        self._lazy_load_collectors()
        return {
            "collectors": [
                smart_collector.get_stats()
                for smart_collector in self.smart_collectors
            ]
        }
