"""Shared helpers and fixtures for collectors unit tests."""

from __future__ import annotations

from unittest.mock import AsyncMock


async def _gemini_cache_with(tokens: dict[str, str] | None = None) -> AsyncMock:
    """Return an AsyncMock token cache returning the provided tokens."""
    mock = AsyncMock()
    mock.get_with_metadata = AsyncMock(
        return_value=(
            tokens or {"gemini_refresh_token": "rt"},
            {"source": "sidecar"},
        )
    )
    return mock
