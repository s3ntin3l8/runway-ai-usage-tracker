"""
Token Cache Service - In-memory cache for sidecar tokens.

Architecture:
- Sidecar extracts tokens from local files and sends to server
- Server stores tokens in memory (30min TTL)
- Server uses tokens to make API calls
- If token expires, sidecar will resend on next run (every 30m)
"""

import time
import logging
import asyncio
from typing import Dict, Optional, Tuple, Any
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class TokenCache:
    """
    In-memory cache for tokens received from sidecars.
    
    Tokens expire after TTL (default 30 minutes = 1800 seconds).
    This maintains stateless philosophy while allowing token reuse.
    """
    
    DEFAULT_TTL = 1800  # 30 minutes
    
    def __init__(self, ttl_seconds: int = DEFAULT_TTL):
        self._cache: Dict[str, Tuple[Dict[str, str], float]] = {}
        self._ttl = ttl_seconds
        self._lock = asyncio.Lock()
    
    async def store(self, provider: str, tokens: Dict[str, str]) -> None:
        """
        Store tokens for a provider.
        
        Args:
            provider: Provider name (e.g., "anthropic", "github")
            tokens: Dict of token type -> value (e.g., {"oauth_token": "abc123"})
        """
        async with self._lock:
            self._cache[provider] = (tokens, time.time())
            logger.info(f"Stored tokens for {provider}: {list(tokens.keys())}")
    
    async def get(self, provider: str) -> Optional[Dict[str, str]]:
        """
        Get tokens if not expired.
        
        Args:
            provider: Provider name
            
        Returns:
            Tokens dict or None if expired/not found
        """
        async with self._lock:
            self._clear_expired_unlocked()
            
            if provider not in self._cache:
                return None
            
            tokens, timestamp = self._cache[provider]
            age = time.time() - timestamp
            
            if age > self._ttl:
                del self._cache[provider]
                logger.debug(f"Token expired for {provider}")
                return None
            
            logger.debug(f"Retrieved tokens for {provider} (age: {age:.0f}s)")
            return tokens
    
    async def get_token(self, provider: str, token_type: str) -> Optional[str]:
        """Get specific token type for provider."""
        tokens = await self.get(provider)
        return tokens.get(token_type) if tokens else None
    
    async def is_valid(self, provider: str) -> bool:
        """Check if provider has valid (non-expired) tokens."""
        return await self.get(provider) is not None
    
    async def get_age(self, provider: str) -> Optional[float]:
        """Get age of tokens in seconds."""
        async with self._lock:
            if provider not in self._cache:
                return None
            _, timestamp = self._cache[provider]
            return time.time() - timestamp
    
    async def get_age_formatted(self, provider: str) -> str:
        """Get formatted age string (e.g., '5m', '2h')."""
        age = await self.get_age(provider)
        if age is None:
            return "unknown"
        
        if age < 60:
            return f"{int(age)}s"
        elif age < 3600:
            return f"{int(age/60)}m"
        else:
            return f"{int(age/3600)}h"
    
    def _clear_expired_unlocked(self) -> None:
        """Clear all expired tokens (assumes lock is held)."""
        now = time.time()
        expired = [
            provider for provider, (_, ts) in self._cache.items()
            if now - ts > self._ttl
        ]
        for provider in expired:
            del self._cache[provider]
            logger.debug(f"Cleared expired tokens for {provider}")

    async def _clear_expired(self) -> None:
        """Thread-safe clear all expired tokens."""
        async with self._lock:
            self._clear_expired_unlocked()

    async def get_all_stats(self) -> Dict[str, Dict[str, Any]]:
        """Get stats for all cached providers."""
        async with self._lock:
            self._clear_expired_unlocked()
            now = time.time()
            return {
                provider: {
                    "tokens": list(tokens.keys()),
                    "age_seconds": int(now - ts),
                    "ttl_remaining": int(self._ttl - (now - ts))
                }
                for provider, (tokens, ts) in self._cache.items()
            }


# Global instance
token_cache = TokenCache()
