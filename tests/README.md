# Testing Guide for AI Usage Tracker

This directory contains comprehensive unit and integration tests for the Runway (AI Usage Tracker) application.

## Quick Start

### Install Testing Dependencies

```bash
pip install -r ../requirements.txt
```

### Run All Tests

```bash
pytest
```

### Run Specific Test Categories

```bash
# Unit tests only
pytest tests/unit/

# Integration tests only
pytest tests/integration/

# Specific test file
pytest tests/unit/test_collectors.py

# Specific test class
pytest tests/unit/test_collectors.py::TestAnthropicCollector

# Specific test function
pytest tests/unit/test_collectors.py::TestAnthropicCollector::test_collect_oauth_success
```

### Run Tests with Coverage Report

```bash
pytest --cov=app --cov-report=html tests/
# Open htmlcov/index.html in browser
```

## Test Structure

### Unit Tests (`tests/unit/`)

Unit tests isolate individual components and test them in isolation with mocked dependencies.

- **test_collectors.py**: Tests for 12 provider collectors (Claude, Gemini, GitHub, ChatGPT, Antigravity, OpenCode, zAI, Kimi, Kimi K2, Kimi Coding, OpenRouter, MiniMax)
  - OAuth API success and failure scenarios
  - Fallback logic between primary and secondary sources
  - Token caching and refresh behavior
  - Error card generation
  - Local log parsing

- **test_config.py**: Configuration loading and validation
  - Environment variable loading
  - Default value application
  - Path expansion
  - Configuration validation

### Integration Tests (`tests/integration/`)

Integration tests verify how components work together end-to-end.

- **test_endpoints.py**: API endpoint testing
  - `/api/limits` endpoint with multiple collectors
  - `/api/ingest` endpoint for external metrics
  - Partial failure handling (some collectors fail, others succeed)
  - Full failure scenarios
  - Response schema validation
  - Error handling and recovery

### Test Fixtures (`tests/conftest.py`)

Shared fixtures used across unit and integration tests:

- **mock_http_client**: Mocked httpx.AsyncClient for API testing
- **mock_*_response**: Pre-defined mock responses from various providers
  - `mock_anthropic_oauth_response`
  - `mock_gemini_quota_response`
  - `mock_github_copilot_response`
  - `mock_chatgpt_usage_response`
  - `mock_opencode_go_response`
  - `mock_zai_response`
  - `mock_kimi_response`

## Running Tests with Markers

Tests are marked with categories for easier filtering:

```bash
# Run only async tests
pytest -m asyncio

# Run only unit tests (if marked)
pytest -m unit

# Skip slow tests
pytest -m "not slow"
```

## Test Coverage Goals

- **Collectors**: 90%+ coverage
  - OAuth/API success and error paths
  - Fallback logic
  - Edge cases (empty responses, invalid data, etc.)

- **Core utilities**: 85%+ coverage
  - Error card generation
  - Timestamp formatting
  - Pace calculation

- **Configuration**: 80%+ coverage
  - Environment variable loading
  - Path resolution
  - Validation

- **Endpoints**: 80%+ coverage
  - Success paths
  - Error scenarios
  - Schema validation

## Best Practices

### Writing New Tests

1. **Use Descriptive Names**: `test_collect_oauth_401_fallback` is better than `test_collect_error`
2. **Test One Thing**: Each test should verify one specific behavior
3. **Use Fixtures**: Leverage shared fixtures instead of duplicating setup
4. **Mock External Dependencies**: Mock API calls, file I/O, etc.
5. **Test Error Cases**: Don't just test the happy path

### Example Test

```python
@pytest.mark.asyncio
async def test_collect_oauth_success(self, mock_http_client, mock_anthropic_oauth_response):
    """Test successful OAuth API collection."""
    collector = AnthropicCollector()
    
    # Setup
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.json.return_value = mock_anthropic_oauth_response
    mock_http_client.get.return_value = mock_response
    
    # Execute
    result = await collector.collect(mock_http_client)
    
    # Verify
    assert isinstance(result, list)
    assert len(result) >= 1
    assert all('service' in card for card in result)
```

## Debugging Tests

### Verbose Output

```bash
pytest -vv tests/unit/test_collectors.py::TestAnthropicCollector::test_collect_oauth_success
```

### Stop on First Failure

```bash
pytest -x tests/
```

### Drop into Debugger on Failure

```bash
pytest --pdb tests/
```

### Show Print Statements

```bash
pytest -s tests/
```

## CI/CD Integration

The project uses GitHub Actions for CI/CD, with workflow files in `.github/workflows/`:

- **Linting**: Checks code style with `ruff` and scans for secrets using `detect-secrets`.
- **Testing**: Runs the full suite with coverage reporting.
- **Build & Push**: Automatically builds and pushes Docker images to **GHCR** on push to `main` or version tags.

To run tests in a similar environment locally:

```bash
pip install -r requirements.txt
pytest --cov=app --cov-report=term-missing tests/
```

## Common Issues and Fixes

### AsyncIO Test Failures

Ensure `pytest-asyncio` is installed and configured in `pytest.ini`:

```ini
[pytest]
asyncio_mode = auto
```

### Import Errors

Make sure the project root is in PYTHONPATH:

```bash
export PYTHONPATH="${PYTHONPATH}:$(pwd)"
pytest
```

### Mock Not Working as Expected

- Verify you're patching the import path where it's used, not where it's defined
- Example: Patch `app.main.AnthropicCollector`, not `app.services.collectors.anthropic.AnthropicCollector`

## Adding Tests for New Collectors

When adding a new collector:

1. Create test class in `tests/unit/test_collectors.py`:
   ```python
   class TestYourCollector:
       @pytest.mark.asyncio
       async def test_collect_success(self, mock_http_client):
           # Test implementation
   ```

2. Add mock response fixture in `tests/conftest.py`:
   ```python
   @pytest.fixture
   def mock_your_response():
       return { ... }
   ```

3. Test both success and failure scenarios:
   - API success
   - API errors (401, 429, 500, etc.)
   - Missing credentials/configuration
   - Fallback mechanisms
   - Malformed responses

## Performance Testing

For performance-critical paths, use pytest-benchmark:

```bash
pip install pytest-benchmark
pytest --benchmark-only tests/
```

## Resources

- [Pytest Documentation](https://docs.pytest.org/)
- [Pytest Async](https://pytest-asyncio.readthedocs.io/)
- [unittest.mock Documentation](https://docs.python.org/3/library/unittest.mock.html)
- [FastAPI Testing](https://fastapi.tiangolo.com/advanced/testing-dependencies/)
