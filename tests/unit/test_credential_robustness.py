import pytest

from app.services.collectors.zai import ZaiCollector


@pytest.mark.asyncio
async def test_is_valid_credential():
    collector = ZaiCollector()

    # Valid keys
    assert collector._is_valid_credential("sk-1234567890") is True
    assert collector._is_valid_credential("some_token_123") is True

    # Invalid keys (None/Empty)
    assert collector._is_valid_credential(None) is False
    assert collector._is_valid_credential("") is False
    assert collector._is_valid_credential("   ") is False

    # Invalid keys (Comments/Placeholders)
    assert collector._is_valid_credential("# api.z.ai → Dashboard [UI]") is False
    assert collector._is_valid_credential("  # some comment") is False
    assert collector._is_valid_credential("# TODO: set key") is False
    assert collector._is_valid_credential("sk-→-placeholder") is False
    assert collector._is_valid_credential("Dashboard [UI]") is False
    assert collector._is_valid_credential("your_key_here_placeholder") is False


@pytest.mark.asyncio
async def test_zai_is_configured_with_comment_fallback():
    collector = ZaiCollector()

    # Mocking _get_api_key to return a comment string
    def mock_get_api_key():
        return "# api.z.ai → Dashboard [UI]"

    collector._get_api_key = mock_get_api_key

    assert await collector.is_configured() is False


@pytest.mark.asyncio
async def test_zai_is_configured_with_valid_key():
    collector = ZaiCollector()

    # Mocking _get_api_key to return a valid key
    def mock_get_api_key():
        return "zai-valid-key-123"

    collector._get_api_key = mock_get_api_key

    assert await collector.is_configured() is True
