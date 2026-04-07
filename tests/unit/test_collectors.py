"""
Unit tests for quota collectors.

Tests cover:
- OAuth/API collection success and error handling
- Fallback logic between primary and secondary sources
- Token caching and refresh behavior
- Error card generation for various failure scenarios
- Local log parsing and file-based data sources
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, mock_open
from datetime import datetime, timezone
import json
import httpx

from app.services.collectors.anthropic import AnthropicCollector
from app.services.collectors.gemini import GeminiCollector
from app.services.collectors.github import GitHubCollector
from app.services.collectors.chatgpt import ChatGPTCollector
from app.services.collectors.antigravity import AntigravityCollector
from app.services.collectors.opencode import OpenCodeCollector
from app.services.collectors.chinese_ai import ChineseAICollector


class TestAnthropicCollector:
    """Test suite for Anthropic (Claude) collector."""

    @pytest.mark.asyncio
    async def test_collect_oauth_success(self, mock_http_client, mock_anthropic_oauth_response):
        """Test successful OAuth API collection."""
        collector = AnthropicCollector()
        
        # Mock successful OAuth response
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = mock_anthropic_oauth_response
        mock_http_client.get.return_value = mock_response
        
        with patch('app.services.collectors.anthropic.settings') as mock_settings:
            mock_settings.CLAUDE_CODE_OAUTH_TOKEN = "test_token"
            mock_settings.CLAUDE_PROJECTS_DIR = "/home/user/.claude/projects"
            
            result = await collector.collect(mock_http_client)
        
        # Should return cards for each quota window
        assert isinstance(result, list)
        assert len(result) >= 1
        assert all('service' in card for card in result)
        assert any('five_hour' in str(card.get('service', '')) or 'Session' in str(card.get('service', '')) for card in result)

    @pytest.mark.asyncio
    async def test_collect_oauth_401_fallback(self, mock_http_client):
        """Test fallback to local logs when OAuth token is invalid (401)."""
        collector = AnthropicCollector()
        
        # Mock 401 response
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 401
        mock_http_client.get.return_value = mock_response
        
        with patch('app.services.collectors.anthropic.settings') as mock_settings:
            mock_settings.CLAUDE_CODE_OAUTH_TOKEN = "invalid_token"
            mock_settings.CLAUDE_PROJECTS_DIR = "/fake/path"
            
            with patch('app.services.collectors.anthropic.glob.glob', return_value=[]):
                result = await collector.collect(mock_http_client)
        
        # Should return error card for invalid token
        assert any('401' in str(card) or 'Invalid' in str(card) for card in result)

    @pytest.mark.asyncio
    async def test_collect_caching(self, mock_http_client, mock_anthropic_oauth_response):
        """Test that OAuth results are cached for 10 minutes."""
        collector = AnthropicCollector()
        
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = mock_anthropic_oauth_response
        mock_http_client.get.return_value = mock_response
        
        with patch('app.services.collectors.anthropic.settings') as mock_settings:
            mock_settings.CLAUDE_CODE_OAUTH_TOKEN = "test_token"
            mock_settings.CLAUDE_PROJECTS_DIR = "/fake/path"
            
            # First call - should hit API
            result1 = await collector.collect(mock_http_client)
            
            # Second call immediately - should use cache
            result2 = await collector.collect(mock_http_client)
            
            # API should only be called once (cached on second call)
            assert mock_http_client.get.call_count == 1
            assert "[Cached]" in str(result2)


class TestGeminiCollector:
    """Test suite for Google Gemini collector."""

    @pytest.mark.asyncio
    async def test_collect_api_success(self, mock_http_client, mock_gemini_quota_response):
        """Test successful Gemini API collection."""
        collector = GeminiCollector()
        
        # Mock responses for both quota and tier endpoints
        quota_response = MagicMock(spec=httpx.Response)
        quota_response.status_code = 200
        quota_response.json.return_value = mock_gemini_quota_response
        
        tier_response = MagicMock(spec=httpx.Response)
        tier_response.status_code = 200
        tier_response.json.return_value = {"tier": "free-tier"}
        
        mock_http_client.post.side_effect = [quota_response, tier_response]
        
        with patch('app.services.collectors.gemini.settings') as mock_settings:
            mock_settings.GEMINI_OAUTH_PATH = "/fake/creds.json"
            mock_settings.GEMINI_SESSIONS_DIR = "/fake/sessions"
            
            with patch('builtins.open', mock_open(read_data=json.dumps({"access_token": "token", "expiry_date": 9999999999999}))):
                with patch('app.services.collectors.gemini.time.time', return_value=1000):
                    result = await collector.collect(mock_http_client)
        
        assert isinstance(result, list)
        assert len(result) >= 1
        assert "Gemini" in str(result[0].get('service', ''))

    @pytest.mark.asyncio
    async def test_collect_missing_credentials(self, mock_http_client):
        """Test graceful handling when credentials file missing."""
        collector = GeminiCollector()
        
        with patch('app.services.collectors.gemini.settings') as mock_settings:
            mock_settings.GEMINI_OAUTH_PATH = "/fake/missing.json"
            mock_settings.GEMINI_SESSIONS_DIR = "/fake/sessions"
            
            with patch('app.services.collectors.gemini.os.path.exists', return_value=False):
                result = await collector.collect(mock_http_client)
        
        # Should return empty list or fallback to logs
        assert isinstance(result, list)


class TestGitHubCollector:
    """Test suite for GitHub Copilot collector."""

    @pytest.mark.asyncio
    async def test_collect_free_tier_quotas(self, mock_http_client, mock_github_copilot_response):
        """Test collection of free tier Copilot quotas."""
        collector = GitHubCollector()
        
        token_response = MagicMock(spec=httpx.Response)
        token_response.status_code = 200
        token_response.json.return_value = mock_github_copilot_response
        
        user_response = MagicMock(spec=httpx.Response)
        user_response.status_code = 200
        user_response.json.return_value = {"quota_snapshots": []}
        
        mock_http_client.get.side_effect = [token_response, user_response]
        
        with patch('app.services.collectors.github.settings') as mock_settings:
            mock_settings.GITHUB_TOKEN = "github_token"
            result = await collector.collect(mock_http_client)
        
        assert isinstance(result, list)
        assert any("Copilot" in str(card.get('service', '')) for card in result)

    @pytest.mark.asyncio
    async def test_collect_missing_token(self, mock_http_client):
        """Test that missing GitHub token returns empty list."""
        collector = GitHubCollector()
        
        with patch('app.services.collectors.github.settings') as mock_settings:
            mock_settings.GITHUB_TOKEN = None
            result = await collector.collect(mock_http_client)
        
        assert result == []


class TestChatGPTCollector:
    """Test suite for ChatGPT collector."""

    @pytest.mark.asyncio
    async def test_collect_api_success(self, mock_http_client, mock_chatgpt_usage_response):
        """Test successful ChatGPT API collection."""
        collector = ChatGPTCollector()
        
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = mock_chatgpt_usage_response
        mock_http_client.get.return_value = mock_response
        
        with patch.dict('os.environ', {'CHATGPT_OAUTH_TOKEN': 'test_token'}):
            result = await collector.collect(mock_http_client)
        
        assert isinstance(result, list)
        assert len(result) >= 1
        assert "ChatGPT" in str(result[0].get('service', ''))
        assert "%" in str(result[0].get('remaining', ''))

    @pytest.mark.asyncio
    async def test_collect_fallback_to_local_logs(self, mock_http_client):
        """Test fallback to local logs when API fails."""
        collector = ChatGPTCollector()
        
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 500
        mock_http_client.get.return_value = mock_response
        
        with patch('app.services.collectors.chatgpt.settings') as mock_settings:
            mock_settings.CHATGPT_SESSIONS_DIR = "/fake/sessions"
            
            with patch('builtins.open', side_effect=FileNotFoundError):
                result = await collector.collect(mock_http_client)
        
        # Should return error card if both API and logs fail
        assert isinstance(result, list)


class TestAntigravityCollector:
    """Test suite for Antigravity IDE collector."""

    @pytest.mark.asyncio
    async def test_collect_file_success(self, mock_http_client):
        """Test successful collection from Antigravity quota file."""
        collector = AntigravityCollector()
        
        quota_data = {
            "models": {
                "claude-3-opus": {
                    "remaining_percent": 65.5,
                    "resets_at": 1744876800
                },
                "claude-3-sonnet": {
                    "remaining_percent": 72.3,
                    "resets_at": 1744876800
                }
            }
        }
        
        with patch('builtins.open', mock_open(read_data=json.dumps(quota_data))):
            with patch('app.services.collectors.antigravity.settings') as mock_settings:
                mock_settings.ANTIGRAVITY_QUOTA_PATH = "/fake/quota.json"
                result = await collector.collect(mock_http_client)
        
        assert isinstance(result, list)
        assert len(result) == 2
        assert all("AG:" in card.get('service', '') for card in result)

    @pytest.mark.asyncio
    async def test_collect_missing_file(self, mock_http_client):
        """Test graceful handling when quota file missing."""
        collector = AntigravityCollector()
        
        with patch('builtins.open', side_effect=FileNotFoundError):
            with patch('app.services.collectors.antigravity.settings') as mock_settings:
                mock_settings.ANTIGRAVITY_QUOTA_PATH = "/fake/missing.json"
                result = await collector.collect(mock_http_client)
        
        # Should return empty list
        assert result == []


class TestOpenCodeCollector:
    """Test suite for OpenCode collector."""

    @pytest.mark.asyncio
    async def test_collect_go_api_success(self, mock_http_client, mock_opencode_go_response):
        """Test successful OpenCode Go API collection."""
        collector = OpenCodeCollector()
        
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}
        mock_response.json.return_value = mock_opencode_go_response
        mock_http_client.get.return_value = mock_response
        
        with patch('app.services.collectors.opencode.settings') as mock_settings:
            mock_settings.OPENCODE_GO_API_KEY = "test_key"
            mock_settings.OPENCODE_DB_PATH = "/fake/db.sqlite"
            
            with patch('app.services.collectors.opencode.os.path.exists', return_value=False):
                result = await collector.collect(mock_http_client)
        
        assert isinstance(result, list)
        assert any("OpenCode Go" in card.get('service', '') for card in result)
        assert "$" in str(result[0].get('remaining', ''))


class TestChineseAICollector:
    """Test suite for Chinese AI providers (zAI and Kimi)."""

    @pytest.mark.asyncio
    async def test_collect_zai_success(self, mock_http_client, mock_zai_response):
        """Test successful zAI (Zhipu) balance collection."""
        collector = ChineseAICollector()
        
        zai_response = MagicMock(spec=httpx.Response)
        zai_response.status_code = 200
        zai_response.json.return_value = mock_zai_response
        
        kimi_response = MagicMock(spec=httpx.Response)
        kimi_response.status_code = 401  # Kimi fails, only zAI succeeds
        
        mock_http_client.get.side_effect = [zai_response, kimi_response]
        
        with patch('app.services.collectors.chinese_ai.settings') as mock_settings:
            mock_settings.ZAI_API_KEY = "zai_key"
            mock_settings.KIMI_API_KEY = "invalid"
            result = await collector.collect(mock_http_client)
        
        assert any("zAI" in card.get('service', '') for card in result)
        assert any("¥" in card.get('remaining', '') for card in result if "zAI" in card.get('service', ''))

    @pytest.mark.asyncio
    async def test_collect_kimi_success(self, mock_http_client, mock_kimi_response):
        """Test successful Kimi balance collection."""
        collector = ChineseAICollector()
        
        zai_response = MagicMock(spec=httpx.Response)
        zai_response.status_code = 401  # zAI fails
        
        kimi_response = MagicMock(spec=httpx.Response)
        kimi_response.status_code = 200
        kimi_response.json.return_value = mock_kimi_response
        
        mock_http_client.get.side_effect = [zai_response, kimi_response]
        
        with patch('app.services.collectors.chinese_ai.settings') as mock_settings:
            mock_settings.ZAI_API_KEY = "invalid"
            mock_settings.KIMI_API_KEY = "kimi_key_valid"
            result = await collector.collect(mock_http_client)
        
        assert any("Kimi" in card.get('service', '') for card in result)
        assert any("$" in card.get('remaining', '') for card in result if "Kimi" in card.get('service', ''))
