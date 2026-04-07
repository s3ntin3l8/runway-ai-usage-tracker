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
        
        # Mock successful OAuth response using request() (called by http_request_with_retry)
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = mock_anthropic_oauth_response
        mock_http_client.request.return_value = mock_response
        
        with patch('app.services.collectors.anthropic.settings') as mock_settings:
            mock_settings.CLAUDE_CODE_OAUTH_TOKEN = "test_token"
            mock_settings.CLAUDE_PROJECTS_DIR = "/home/user/.claude/projects"
            
            with patch('app.services.collectors.anthropic.get_claude_session_cookie', return_value=None):
                result = await collector.collect(mock_http_client)
        
        # Should return cards for each quota window
        assert isinstance(result, list)
        assert len(result) >= 1
        assert all('service' in card for card in result)
        assert any('Session' in str(card.get('service', '')) for card in result)

    @pytest.mark.asyncio
    async def test_collect_oauth_401_fallback(self, mock_http_client):
        """Test fallback to local logs when OAuth token is invalid (401)."""
        collector = AnthropicCollector()
        
        # Mock 401 response using request()
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 401
        mock_http_client.request.return_value = mock_response
        
        with patch('app.services.collectors.anthropic.settings') as mock_settings:
            mock_settings.CLAUDE_CODE_OAUTH_TOKEN = "invalid_token"
            mock_settings.CLAUDE_PROJECTS_DIR = "/fake/path"
            
            with patch('app.services.collectors.anthropic.glob.glob', return_value=[]):
                with patch('app.services.collectors.anthropic.get_claude_session_cookie', return_value=None):
                    result = await collector.collect(mock_http_client)
        
        # Should return error card for invalid token (no logs fallback)
        assert any('ERR' in str(card.get('remaining', '')) for card in result)

    @pytest.mark.asyncio
    async def test_collect_caching(self, mock_http_client, mock_anthropic_oauth_response):
        """Test that OAuth results are cached for 10 minutes."""
        collector = AnthropicCollector()
        
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = mock_anthropic_oauth_response
        mock_http_client.request.return_value = mock_response
        
        with patch('app.services.collectors.anthropic.settings') as mock_settings:
            mock_settings.CLAUDE_CODE_OAUTH_TOKEN = "test_token"
            mock_settings.CLAUDE_PROJECTS_DIR = "/fake/path"
            
            with patch('app.services.collectors.anthropic.get_claude_session_cookie', return_value=None):
                # First call - should hit API
                result1 = await collector.collect(mock_http_client)
                
                # Second call immediately - should use cache
                result2 = await collector.collect(mock_http_client)
                
                # API should only be called once (cached on second call)
                assert mock_http_client.request.call_count == 1
                assert "[Cached]" in str(result2)

    @pytest.mark.asyncio
    async def test_collect_web_api_fallback(self, mock_http_client, mock_claude_web_api_orgs_response, mock_claude_web_api_usage_response):
        """Test fallback to Web API when OAuth fails."""
        collector = AnthropicCollector()
        
        # Mock OAuth failure (401) - using request() for OAuth
        oauth_response = MagicMock(spec=httpx.Response)
        oauth_response.status_code = 401
        
        # Mock Web API success - using get() for Web API
        orgs_response = MagicMock(spec=httpx.Response)
        orgs_response.status_code = 200
        orgs_response.json.return_value = mock_claude_web_api_orgs_response
        
        usage_response = MagicMock(spec=httpx.Response)
        usage_response.status_code = 200
        usage_response.json.return_value = mock_claude_web_api_usage_response
        
        # Mock request for OAuth (first call)
        mock_http_client.request.return_value = oauth_response
        # Mock get for Web API calls
        mock_http_client.get.side_effect = [orgs_response, usage_response]
        
        with patch('app.services.collectors.anthropic.settings') as mock_settings:
            mock_settings.CLAUDE_CODE_OAUTH_TOKEN = "invalid_token"
            mock_settings.CLAUDE_PROJECTS_DIR = "/fake/path"
            
            with patch('app.services.collectors.anthropic.get_claude_session_cookie', return_value="sk-ant-session123"):
                result = await collector.collect(mock_http_client)
        
        # Should return Web API results
        assert isinstance(result, list)
        assert len(result) >= 1
        assert any('Web API' in str(card.get('detail', '')) for card in result)
        assert any('Session' in str(card.get('service', '')) for card in result)

    @pytest.mark.asyncio
    async def test_collect_enhanced_local_fallback(self, mock_http_client):
        """Test fallback to enhanced local logs when both OAuth and Web API fail."""
        collector = AnthropicCollector()
        
        # Mock OAuth failure
        oauth_response = MagicMock(spec=httpx.Response)
        oauth_response.status_code = 401
        mock_http_client.get.return_value = oauth_response
        
        # Mock no web cookie
        with patch('app.services.collectors.anthropic.settings') as mock_settings:
            mock_settings.CLAUDE_CODE_OAUTH_TOKEN = "invalid_token"
            
            with patch('app.services.collectors.anthropic.get_claude_session_cookie', return_value=None):
                # Mock local log data with all token types
                log_data = [
                    json.dumps({
                        "type": "assistant",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "message": {
                            "id": "msg_1",
                            "requestId": "req_1",
                            "usage": {
                                "input_tokens": 1000,
                                "output_tokens": 500,
                                "cache_read_tokens": 2000,
                                "cache_creation_tokens": 100
                            }
                        }
                    }) + "\n",
                    json.dumps({
                        "type": "assistant",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "message": {
                            "id": "msg_2",  # Different message, should be counted
                            "requestId": "req_2",
                            "usage": {
                                "input_tokens": 500,
                                "output_tokens": 200,
                                "cache_read_tokens": 0,
                                "cache_creation_tokens": 0
                            }
                        }
                    }) + "\n"
                ]
                
                with patch('builtins.open', mock_open(read_data=''.join(log_data))):
                    with patch('app.services.collectors.anthropic.glob.glob', return_value=["/fake/path/test.jsonl"]):
                        with patch('os.path.isdir', return_value=True):
                            result = await collector.collect(mock_http_client)
        
        # Should return local log results
        assert isinstance(result, list)
        assert len(result) == 1
        assert 'Claude Pro' in str(result[0].get('service', ''))
        assert 'Local Logs' in str(result[0].get('detail', ''))
        # Should sum all token types: (1000+500+2000+100) + (500+200+0+0) = 4300
        assert '4,300' in str(result[0].get('detail', '')) or '4300' in str(result[0].get('detail', ''))

    @pytest.mark.asyncio
    async def test_collect_local_dedup(self, mock_http_client):
        """Test deduplication of streaming chunks in local logs."""
        collector = AnthropicCollector()
        
        # Mock OAuth failure
        oauth_response = MagicMock(spec=httpx.Response)
        oauth_response.status_code = 401
        mock_http_client.get.return_value = oauth_response
        
        # Mock local log data with duplicate messages (streaming chunks)
        log_data = [
            json.dumps({
                "type": "assistant",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "message": {
                    "id": "msg_dup",
                    "requestId": "req_dup",
                    "usage": {
                        "input_tokens": 1000,
                        "output_tokens": 500,
                        "cache_read_tokens": 0,
                        "cache_creation_tokens": 0
                    }
                }
            }) + "\n",
            json.dumps({
                "type": "assistant",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "message": {
                    "id": "msg_dup",  # Same ID - should be deduplicated
                    "requestId": "req_dup",  # Same requestId
                    "usage": {
                        "input_tokens": 1000,
                        "output_tokens": 500,
                        "cache_read_tokens": 0,
                        "cache_creation_tokens": 0
                    }
                }
            }) + "\n"
        ]
        
        with patch('app.services.collectors.anthropic.settings') as mock_settings:
            mock_settings.CLAUDE_CODE_OAUTH_TOKEN = "invalid_token"
            
            with patch('app.services.collectors.anthropic.get_claude_session_cookie', return_value=None):
                with patch('builtins.open', mock_open(read_data=''.join(log_data))):
                    with patch('app.services.collectors.anthropic.glob.glob', return_value=["/fake/path/test.jsonl"]):
                        with patch('os.path.isdir', return_value=True):
                            result = await collector.collect(mock_http_client)
        
        # Should deduplicate - only count once
        assert isinstance(result, list)
        assert len(result) == 1
        # Should only show 1500 tokens (not 3000 from duplicate)
        detail = str(result[0].get('detail', ''))
        assert '1,500' in detail or '1500' in detail

    @pytest.mark.asyncio
    async def test_collect_multi_config_dirs(self, mock_http_client):
        """Test scanning multiple config directories via CLAUDE_CONFIG_DIR."""
        collector = AnthropicCollector()
        
        # Mock OAuth failure
        oauth_response = MagicMock(spec=httpx.Response)
        oauth_response.status_code = 401
        mock_http_client.get.return_value = oauth_response
        
        with patch('app.services.collectors.anthropic.settings') as mock_settings:
            mock_settings.CLAUDE_CODE_OAUTH_TOKEN = "invalid_token"
            
            with patch('app.services.collectors.anthropic.get_claude_session_cookie', return_value=None):
                with patch.dict('os.environ', {'CLAUDE_CONFIG_DIR': '/path1,/path2'}):
                    with patch('os.path.isdir', return_value=True):
                        with patch('app.services.collectors.anthropic.glob.glob') as mock_glob:
                            # Return files from both paths
                            def glob_side_effect(pattern, **kwargs):
                                if '/path1' in pattern:
                                    return ['/path1/projects/file1.jsonl']
                                elif '/path2' in pattern:
                                    return ['/path2/projects/file2.jsonl']
                                return []
                            
                            mock_glob.side_effect = glob_side_effect
                            
                            # Mock file contents
                            log_data_1 = json.dumps({
                                "type": "assistant",
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                                "message": {
                                    "id": "msg_1",
                                    "requestId": "req_1",
                                    "usage": {"input_tokens": 1000, "output_tokens": 500}
                                }
                            }) + "\n"
                            
                            log_data_2 = json.dumps({
                                "type": "assistant",
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                                "message": {
                                    "id": "msg_2",
                                    "requestId": "req_2",
                                    "usage": {"input_tokens": 500, "output_tokens": 200}
                                }
                            }) + "\n"
                            
                            def open_side_effect(path, **kwargs):
                                if 'file1' in path:
                                    return mock_open(read_data=log_data_1)()
                                else:
                                    return mock_open(read_data=log_data_2)()
                            
                            with patch('builtins.open', side_effect=open_side_effect):
                                result = await collector.collect(mock_http_client)
        
        # Should aggregate from both directories
        assert isinstance(result, list)
        assert len(result) == 1


class TestGeminiCollector:
    """Test suite for Google Gemini collector."""

    @pytest.mark.asyncio
    async def test_collect_api_success(self, mock_http_client, mock_gemini_quota_response):
        """Test successful Gemini API collection with project discovery."""
        collector = GeminiCollector()
        
        # Mock responses - tier request comes FIRST (to get project ID)
        tier_response = MagicMock(spec=httpx.Response)
        tier_response.status_code = 200
        tier_response.json.return_value = {
            "currentTier": {"id": "standard-tier", "name": "Gemini Code Assist"},
            "cloudaicompanionProject": "test-project-123"
        }
        
        quota_response = MagicMock(spec=httpx.Response)
        quota_response.status_code = 200
        quota_response.json.return_value = mock_gemini_quota_response
        
        # Create async mock that returns responses in order
        call_count = [0]
        async def mock_post(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return tier_response  # First call: loadCodeAssist
            else:
                return quota_response  # Second call: retrieveUserQuota
        
        mock_http_client.post = mock_post
        
        with patch('app.services.collectors.gemini.settings') as mock_settings:
            mock_settings.GEMINI_OAUTH_PATH = "/fake/creds.json"
            mock_settings.GEMINI_SESSIONS_DIR = "/fake/sessions"
            
            with patch('builtins.open', mock_open(read_data=json.dumps({"access_token": "token", "expiry_date": 9999999999999}))):
                with patch('app.services.collectors.gemini.os.path.exists', return_value=True):
                    with patch('app.services.collectors.gemini.time.time', return_value=1000):
                        result = await collector.collect(mock_http_client)
        
        assert isinstance(result, list)
        assert len(result) >= 1
        # Should return one card per model bucket
        assert len(result) == len(mock_gemini_quota_response["buckets"])
        # Check that service name contains model identifier (either display name or raw model ID)
        assert any(name in str(result[0].get('service', '')) for name in ['Gemini', 'gemini'])
        # Verify health field exists
        assert 'health' in result[0]
        # Verify unit is "used" (not "quota")
        assert result[0].get('unit') == 'used'
        # Verify project was used in quota call
        assert call_count[0] == 2  # Should make 2 API calls

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
    async def test_collect_returns_list(self, mock_http_client):
        """Test OpenCode collector returns a list (may be empty if no data sources available)."""
        collector = OpenCodeCollector()
        
        # Mock all external dependencies to simulate no data available
        with patch('app.services.collectors.opencode.get_opencode_session_cookie', return_value=None):
            with patch('app.services.collectors.opencode.external_metric_service') as mock_external:
                mock_external.get_opencode_aggregated.return_value = []
                
                # Mock local DB doesn't exist
                with patch('app.services.collectors.opencode.os.path.exists', return_value=False):
                    result = await collector.collect(mock_http_client)
        
        assert isinstance(result, list)
        # When no data sources are available, should return empty list
        assert result == []


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
