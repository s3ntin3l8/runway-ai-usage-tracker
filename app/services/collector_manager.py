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
from app.services.collectors.zai import ZaiCollector
from app.services.collectors.kimi_code import KimiCodeCollector
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
        """Initialize all collectors wrapped with SmartCollector."""
        # Define collectors with names and TTL values
        collector_configs = [
            (AnthropicCollector(), "Claude (Anthropic)", 600),      # 10 min
            (GeminiCollector(), "Gemini", 300),                     # 5 min
            (GitHubCollector(), "GitHub Copilot", 900),             # 15 min
            (ChatGPTCollector(), "ChatGPT", 600),                   # 10 min
            (AntigravityCollector(), "Antigravity", 900),           # 15 min
            (OpenCodeCollector(), "OpenCode", 1800),                # 30 min
            (ZaiCollector(), "zAI", 900),                           # 15 min
            (KimiCodeCollector(), "Kimi Code", 900)                 # 15 min
        ]
        
        # Wrap each collector with SmartCollector for differential fetching
        self.smart_collectors = [
            SmartCollector(
                collector=collector,
                collector_name=name,
                ttl=ttl,
                error_threshold=3,      # Force retry after 3 consecutive errors
                error_retry_delay=30.0  # Wait 30s before retrying after error
            )
            for collector, name, ttl in collector_configs
        ]
        
        logger.info(f"CollectorManager initialized with {len(self.smart_collectors)} collectors")
    
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
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Run all collectors concurrently with exception handling
            tasks = [
                smart_collector.collect(client)
                for smart_collector in self.smart_collectors
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
        
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
        return {
            "collectors": [
                smart_collector.get_stats()
                for smart_collector in self.smart_collectors
            ]
        }
