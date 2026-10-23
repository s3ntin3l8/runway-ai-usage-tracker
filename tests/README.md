# Testing Guide for AI Usage Tracker

This directory contains comprehensive unit and integration tests for the Runway (AI Usage Tracker) application.

## Quick Start

### Install Dependencies

```bash
make install    # from the repo root: venv, Python deps, webapp deps, git hooks
```

### Run All Tests

```bash
make test       # or: pytest
```

`make test` runs the suite in four parallel workers. Override with
`make test JOBS=1` for a serial run (easier to read a single traceback) or
`make test JOBS=8` on a bigger machine.

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

- **test_collectors.py**: Tests for the API/OAuth collectors (Claude, Gemini, GitHub Copilot, ChatGPT, DeepSeek, Kimi API, Kimi K2, Kimi Coding, MiniMax, OpenCode, OpenRouter, zAI) — dedicated collectors have their own files (`test_antigravity_collector.py`, `test_collectors_xai.py`, ...)
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
  - `/api/v1/usage/limits` endpoint with multiple collectors
  - `/api/v1/fleet/ingest` endpoint for sidecar-pushed metrics
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
  - `mock_chatgpt_usage_response`
  - `mock_opencode_go_response`
  - `mock_zai_response`
  - `mock_kimi_response`

### SQLite writes skip fsyncing (`tests/fixtures/sqlite.py`)

The suite builds its own throwaway SQLite engines rather than reusing the app
engine, and those engines used to keep SQLite's default `synchronous=FULL` —
an fsync on every commit. That made the suite I/O-bound: roughly 80% of the
local run's wall time was spent waiting on the filesystem instead of running
code.

A session-scoped fixture in `tests/conftest.py` now turns fsyncing off for
the suite's own engines. The app's engine is exempt — it sets
`synchronous=NORMAL` and WAL on purpose, and the suite must not change a
production durability decision to buy speed.

Set `RUNWAY_TEST_SQLITE_SYNC=1` to restore the durable default for a run.

### Running in parallel

The suite runs in four workers (`make test`, or `--jobs N` on
`scripts/dev_checks.py`). Workers split by *file*, so a module's
module-scoped fixtures and anything it sets up at import time stay together.

That means collection has to be reproducible: a parametrised value that
changes between processes produces a different test id in each worker, and
pytest-xdist rejects the run with *"different tests were collected between
gw0 and gw1"*. If you add a parametrised argument, keep it free of the
clock, randomness and other per-process state — or give it an explicit
`pytest.param(..., id="...")`.

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

CI enforces overall floors of **70%** (Python, via `test-python`) and
**85%** (frontend, via `test-frontend`); per-area targets above those floors:

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
    assert all("service" in card for card in result)
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

### A Worker That Never Reports

On Windows, a pytest-xdist worker can stall inside CPython's
`socket.socketpair()`, which is what both asyncio event loops build their
self-pipe from. The worker never finishes, xdist prints `node down: Not
properly terminated`, and the master dies with a `KeyError` while rescheduling
the worker it lost. The captured stack is in
[#830](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/830).

The test session replaces `socket.socketpair` with one that cannot hang:
`tests/windows_socketpair.py` connects in blocking mode, so the handshake
completes before the `accept()` that could otherwise wait forever. It is
installed by `tests/conftest.py` at import time, and only on Windows, and only
for a CPython that has no native socketpair there. Nothing outside the test
session is affected.

To watch what a stalled worker is doing, dump every thread's stack every two
minutes:

```bash
RUNWAY_TEST_STACKDUMP=1 pytest -n 4 --dist loadfile tests/
```

To put CPython's own socketpair back while debugging something else:

```bash
RUNWAY_DISABLE_WINDOWS_SOCKETPAIR=1 pytest tests/
```

## CI/CD Integration

The project uses GitHub Actions for CI/CD, with workflow files in `.github/workflows/` (full detail in [`docs/architecture.md`](../docs/architecture.md#cicd)):

- **Linting**: ruff (style/format) plus mypy, detect-secrets, and pip-audit. mypy and pip-audit are hard gates on the Linux job (`strict-checks: true`).
- **Testing**: `test-python` runs pytest with coverage (floor 85%) in four parallel workers; `test-frontend` runs vitest (floor 85%).
- **Native Windows**: `windows` runs the static checks and `windows/specific` runs the modules whose subject is the Windows platform. The full suite runs on a Windows runner on `main` pushes and nightly (`windows-native-suite.yml`), as four parallel shards (see `scripts/pytest_shard.py`).
- **Up to date with `main`**: the required checks are *not* required to have run against the
  current tip of `main`, so a pull request does not need a rebase to merge — and a rebase is
  what re-triggered all five pull-request workflows. The merge queue, which would test the
  merge commit itself, needs the repository to be owned by an organization; see
  [`docs/architecture.md`](../docs/architecture.md#cicd).
- **Build & Push**: `build-docker` pushes the `:edge` image to GHCR on push to `main`; releases publish `:latest` + the version tag.

To run tests the way CI does:

```bash
make lint && make test
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
       return {...}
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
