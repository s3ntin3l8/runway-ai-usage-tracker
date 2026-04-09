# Critical Bug Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 5 critical bugs identified in code review: a token-redaction write-back omission, an HMAC guard, two sidecar crashes (queue and Windows cred cache), and blocking I/O on the async event loop.

**Architecture:** Each fix is surgical — one bug per task. Tests are written first (TDD), then the minimal code change is applied and verified. No refactoring beyond what's needed to fix the bug.

**Tech Stack:** Python 3.9+, FastAPI, pytest, asyncio, hmac, sqlite3

---

## Files Modified

| File | Task | Change |
|------|------|--------|
| `app/api/endpoints/ingest.py` | T1, T2 | Redaction write-back + HMAC guard |
| `scripts/sidecar.py` | T3, T4 | queue_rotate default + _windows_cred_cache init |
| `app/services/collectors/anthropic.py` | T5 | Offload blocking I/O to thread |
| `tests/integration/test_endpoints.py` | T1, T2 | New ingest test cases |
| `tests/unit/test_sidecar.py` | T3, T4 | New sidecar unit test file |
| `tests/unit/test_collectors.py` | T5 | New async blocking-IO test |

---

## Task 1: Fix C2 — OAuth token redaction not written back to card.detail

**Problem:** In `app/api/endpoints/ingest.py` lines 87–91, when a card has an `oauth_token` but no `refresh_token`, `detail` is updated locally but `card.detail` is never written back. The raw token persists on the card object.

**Files:**
- Modify: `app/api/endpoints/ingest.py:87-91`
- Test: `tests/integration/test_endpoints.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/integration/test_endpoints.py` inside `class TestIngestEndpoint`:

```python
async def test_ingest_oauth_token_redacted_no_refresh_token(self):
    """C2: card.detail must be redacted when oauth_token present but refresh_token absent."""
    from fastapi.testclient import TestClient

    test_client = TestClient(app)

    oauth_token = "sk-ant-oauthtest123"
    payload = {
        "provider": "anthropic",
        "metrics": [
            {
                "service": "Claude Pro",
                "icon": "🟠",
                "remaining": "60%",
                "unit": "capacity",
                "reset": "in 3h",
                "health": "good",
                "pace": "~5 days",
                "detail": f"oauth_token:{oauth_token} some other data"
            }
        ]
    }

    body = json.dumps(payload)
    headers = self._get_hmac_headers(body)

    stored_cards = []

    def capture_metrics(provider, data):
        stored_cards.extend(data.get("cards", []))

    with patch('app.api.endpoints.ingest.external_metric_service') as mock_service:
        mock_service.metrics = {}
        def save_side_effect():
            pass
        mock_service._save = MagicMock(side_effect=save_side_effect)

        # Capture what gets stored
        def set_metrics(provider, value):
            mock_service.metrics[provider] = value

        mock_service.metrics.__setitem__ = MagicMock(side_effect=lambda k, v: stored_cards.extend(v.get("cards", [])))

        with patch('app.api.endpoints.ingest.token_cache'):
            response = test_client.post("/api/ingest", content=body, headers=headers)

    assert response.status_code == 200
    # The raw oauth token must not appear in any stored card detail
    for card in stored_cards:
        assert oauth_token not in card.get("detail", ""), \
            f"Raw oauth_token found in stored card detail: {card['detail']}"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /home/bjoern/projects/ai-usage-tracker && source .venv/bin/activate && pytest tests/integration/test_endpoints.py::TestIngestEndpoint::test_ingest_oauth_token_redacted_no_refresh_token -v
```

Expected: FAIL (raw token present in stored card)

- [ ] **Step 3: Fix the write-back**

In `app/api/endpoints/ingest.py`, change lines 87–91 from:

```python
        # Redact tokens from detail string AFTER both are extracted
        if oauth_token:
            detail = detail.replace(f"oauth_token:{oauth_token}", "oauth_token:[REDACTED]")
        if refresh_token:
            detail = detail.replace(f"refresh_token:{refresh_token}", "refresh_token:[REDACTED]")
            card.detail = detail
```

To:

```python
        # Redact tokens from detail string AFTER both are extracted
        if oauth_token:
            detail = detail.replace(f"oauth_token:{oauth_token}", "oauth_token:[REDACTED]")
        if refresh_token:
            detail = detail.replace(f"refresh_token:{refresh_token}", "refresh_token:[REDACTED]")
        if oauth_token or refresh_token:
            card.detail = detail
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/integration/test_endpoints.py::TestIngestEndpoint::test_ingest_oauth_token_redacted_no_refresh_token -v
```

Expected: PASS

- [ ] **Step 5: Run full test suite to check no regressions**

```bash
pytest tests/ -v --tb=short 2>&1 | tail -30
```

Expected: all previously-passing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add app/api/endpoints/ingest.py tests/integration/test_endpoints.py
git commit -m "fix(ingest): always write back redacted detail when oauth_token present without refresh_token"
```

---

## Task 2: Fix C1 — Guard HMAC computation against empty INGEST_API_KEY

**Problem:** If `INGEST_API_KEY` is set to an empty string via env var, the ingest endpoint will accept any signature (HMAC with an empty key is deterministic and guessable). A guard should reject requests immediately with a 503 when the key is empty, rather than silently accepting insecure signatures.

**Files:**
- Modify: `app/api/endpoints/ingest.py:45-55`
- Test: `tests/integration/test_endpoints.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/integration/test_endpoints.py` inside `class TestIngestEndpoint`:

```python
async def test_ingest_rejects_when_api_key_empty(self):
    """C1: ingest endpoint must return 503 when INGEST_API_KEY is empty."""
    from fastapi.testclient import TestClient

    test_client = TestClient(app)

    payload = {"provider": "claude", "metrics": []}
    body = json.dumps(payload)
    timestamp = str(int(time.time()))
    # Sign with empty key (what attacker would do)
    sig = hmac.new(b"", (timestamp + body).encode(), hashlib.sha256).hexdigest()
    headers = {
        "X-Signature": sig,
        "X-Timestamp": timestamp,
        "Content-Type": "application/json",
    }

    with patch('app.api.endpoints.ingest.settings') as mock_settings:
        mock_settings.INGEST_API_KEY = ""
        response = test_client.post("/api/ingest", content=body, headers=headers)

    assert response.status_code == 503
    assert "not configured" in response.json().get("detail", "").lower()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/integration/test_endpoints.py::TestIngestEndpoint::test_ingest_rejects_when_api_key_empty -v
```

Expected: FAIL (currently returns 200 or 401, not 503)

- [ ] **Step 3: Add the guard at the top of the ingest handler**

In `app/api/endpoints/ingest.py`, after line 31 (after the missing-header check), add:

```python
    # 0. Guard against misconfigured empty API key
    if not settings.INGEST_API_KEY:
        logger.error("INGEST_API_KEY is empty — ingest endpoint is disabled")
        raise HTTPException(status_code=503, detail="Ingest endpoint not configured: INGEST_API_KEY is empty")
```

Place this block immediately after the docstring, before the existing `# 1. Check headers` block:

```python
@router.post("/ingest")
async def ingest_metrics(
    raw_request: Request,
    x_signature: str = Header(None, alias="X-Signature"),
    x_timestamp: str = Header(None, alias="X-Timestamp")
):
    """..."""
    # 0. Guard against misconfigured empty API key
    if not settings.INGEST_API_KEY:
        logger.error("INGEST_API_KEY is empty — ingest endpoint is disabled")
        raise HTTPException(status_code=503, detail="Ingest endpoint not configured: INGEST_API_KEY is empty")

    # 1. Check headers
    if not x_signature or not x_timestamp:
        ...
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/integration/test_endpoints.py::TestIngestEndpoint::test_ingest_rejects_when_api_key_empty -v
```

Expected: PASS

- [ ] **Step 5: Run full test suite**

```bash
pytest tests/ -v --tb=short 2>&1 | tail -30
```

Expected: all previously-passing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add app/api/endpoints/ingest.py tests/integration/test_endpoints.py
git commit -m "fix(ingest): return 503 when INGEST_API_KEY is empty to prevent insecure HMAC acceptance"
```

---

## Task 3: Fix C3 — queue_rotate crash when called with no arguments

**Problem:** In `scripts/sidecar.py`, `queue_push` calls `queue_rotate()` with no arguments (line 266). Inside `queue_rotate`, when both `max_size_mb` and `config` are `None`, the code reaches `max_size_bytes = None * 1024 * 1024` → `TypeError`. The offline queue (the safety net for when the server is down) silently crashes on every invocation.

**Files:**
- Modify: `scripts/sidecar.py:269`
- Test: `tests/unit/test_sidecar.py` (new file)

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_sidecar.py`:

```python
"""Unit tests for sidecar critical bug fixes."""
import sys
import os
import json
import time
import tempfile
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Import sidecar as a module (it's a script in scripts/)
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
import sidecar


class TestQueueRotate:
    """C3: queue_rotate must not crash when called with no arguments."""

    def test_queue_rotate_no_args_does_not_crash(self, tmp_path):
        """queue_rotate() with no args must not raise TypeError."""
        with patch.object(sidecar, 'get_queue_dir', return_value=tmp_path):
            # Should not raise — previously crashed with TypeError: NoneType * 1024
            sidecar.queue_rotate()

    def test_queue_rotate_no_args_uses_default_10mb_limit(self, tmp_path):
        """queue_rotate() with no args uses 10 MB as the size limit."""
        # Write a small file — should NOT be rotated (well under 10 MB)
        queue_file = tmp_path / "2026-01-01.jsonl"
        queue_file.write_text('{"ts": 1, "payload": {}}\n')

        with patch.object(sidecar, 'get_queue_dir', return_value=tmp_path):
            sidecar.queue_rotate()

        # Small file must survive
        assert queue_file.exists()

    def test_queue_push_does_not_crash(self, tmp_path):
        """queue_push must successfully queue a payload without crashing."""
        with patch.object(sidecar, 'get_queue_dir', return_value=tmp_path):
            with patch.object(sidecar, 'ensure_dirs'):
                # Must not raise
                sidecar.queue_push({"provider": "test", "metrics": []})

        files = list(tmp_path.glob("*.jsonl"))
        assert len(files) == 1
        entry = json.loads(files[0].read_text().strip())
        assert entry["payload"] == {"provider": "test", "metrics": []}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /home/bjoern/projects/ai-usage-tracker && source .venv/bin/activate && pytest tests/unit/test_sidecar.py::TestQueueRotate -v
```

Expected: FAIL — `TypeError: unsupported operand type(s) for *: 'NoneType' and 'int'`

- [ ] **Step 3: Fix the default parameter**

In `scripts/sidecar.py`, change line 269 from:

```python
def queue_rotate(max_size_mb: Optional[int] = None, config: Optional[Dict[str, Any]] = None) -> None:
```

To:

```python
def queue_rotate(max_size_mb: int = 10, config: Optional[Dict[str, Any]] = None) -> None:
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/unit/test_sidecar.py::TestQueueRotate -v
```

Expected: all 3 tests PASS

- [ ] **Step 5: Run full test suite**

```bash
pytest tests/ -v --tb=short 2>&1 | tail -30
```

Expected: all previously-passing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/sidecar.py tests/unit/test_sidecar.py
git commit -m "fix(sidecar): set default max_size_mb=10 in queue_rotate to prevent TypeError crash when called without args"
```

---

## Task 4: Fix C4 — _windows_cred_cache initialized as None causes write crash

**Problem:** In `scripts/sidecar.py` line 55, `_windows_cred_cache` is initialized to `None`. On the first call to `get_windows_credential` on Windows, the read guard (`if _windows_cred_cache is not None`) correctly skips the cache read, but the write at line 555 (`_windows_cred_cache[target] = ...`) crashes with `TypeError: 'NoneType' object does not support item assignment`.

**Files:**
- Modify: `scripts/sidecar.py:55`
- Test: `tests/unit/test_sidecar.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_sidecar.py`:

```python
class TestWindowsCredCache:
    """C4: _windows_cred_cache must be a dict, not None, to support item assignment."""

    def test_windows_cred_cache_is_dict_not_none(self):
        """_windows_cred_cache must be initialized as a dict."""
        assert isinstance(sidecar._windows_cred_cache, dict), \
            f"_windows_cred_cache is {type(sidecar._windows_cred_cache)}, expected dict"

    def test_get_windows_credential_write_does_not_crash(self):
        """Writing to _windows_cred_cache must not raise TypeError."""
        # Simulate the write that happens after a successful PowerShell call
        # This must not crash regardless of platform
        try:
            sidecar._windows_cred_cache["test_target"] = ("password", time.time() + 300)
        except TypeError as e:
            pytest.fail(f"Writing to _windows_cred_cache raised TypeError: {e}")
        finally:
            # Clean up
            sidecar._windows_cred_cache.pop("test_target", None)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/unit/test_sidecar.py::TestWindowsCredCache -v
```

Expected: `test_windows_cred_cache_is_dict_not_none` FAILS because `_windows_cred_cache` is `None`

- [ ] **Step 3: Fix the initialization**

In `scripts/sidecar.py`, change line 55 from:

```python
_windows_cred_cache: Optional[dict] = None  # cache {target: password, ttl: timestamp}s
```

To:

```python
_windows_cred_cache: dict = {}  # cache {target: (password, ttl_timestamp)}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/unit/test_sidecar.py::TestWindowsCredCache -v
```

Expected: both tests PASS

- [ ] **Step 5: Run full test suite**

```bash
pytest tests/ -v --tb=short 2>&1 | tail -30
```

Expected: all previously-passing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/sidecar.py tests/unit/test_sidecar.py
git commit -m "fix(sidecar): initialize _windows_cred_cache as empty dict instead of None to prevent TypeError on write"
```

---

## Task 5: Fix C5 — Blocking synchronous I/O inside async _get_claude_local_enhanced

**Problem:** `AnthropicCollector._get_claude_local_enhanced()` is declared `async` but contains purely synchronous operations: `glob.glob()`, `open()`, and line-by-line reads of potentially hundreds of large `.jsonl` files. This blocks the uvicorn event loop for the full duration of the file scan, starving all concurrent requests and pushing other collectors towards the 20-second timeout.

**Fix:** Extract the synchronous I/O body into a private sync method `_get_claude_local_enhanced_sync()` and run it via `asyncio.to_thread()`.

**Files:**
- Modify: `app/services/collectors/anthropic.py:722-854`
- Test: `tests/unit/test_collectors.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_collectors.py` inside `class TestAnthropicCollector`:

```python
@pytest.mark.asyncio
async def test_get_claude_local_enhanced_is_nonblocking(self):
    """C5: _get_claude_local_enhanced must not block the event loop during file I/O."""
    import asyncio
    collector = AnthropicCollector()

    # A sync-blocking implementation would prevent this coroutine from running
    # concurrently. We verify by running both concurrently and checking neither hangs.
    async def noop_task():
        await asyncio.sleep(0)
        return "noop_done"

    with patch('app.services.collectors.anthropic.glob.glob', return_value=[]):
        with patch('app.services.collectors.anthropic.settings') as mock_settings:
            mock_settings.CLAUDE_PRO_LIMIT = 2000000
            mock_settings.CLAUDE_FREE_LIMIT = 500000
            mock_settings.CLAUDE_PROJECTS_DIR = ""

            # Run both concurrently — if _get_claude_local_enhanced blocks,
            # noop_task cannot complete until it finishes.
            result, noop = await asyncio.gather(
                collector._get_claude_local_enhanced(),
                noop_task(),
                return_exceptions=True
            )

    assert noop == "noop_done", "Event loop was blocked — noop_task did not run concurrently"
    # No files found, so result should be None or empty list
    assert result is None or result == []
```

- [ ] **Step 2: Run test to verify it currently passes (baseline)**

```bash
pytest tests/unit/test_collectors.py::TestAnthropicCollector::test_get_claude_local_enhanced_is_nonblocking -v
```

Note: This test will currently PASS because the mock makes the method fast (no real I/O). The real issue manifests under load with actual files. We need a test that detects blocking. Use a more targeted approach — verify `asyncio.to_thread` is called:

Replace the test above with:

```python
@pytest.mark.asyncio
async def test_get_claude_local_enhanced_uses_to_thread(self):
    """C5: _get_claude_local_enhanced must delegate sync I/O to asyncio.to_thread."""
    import asyncio
    collector = AnthropicCollector()

    with patch('app.services.collectors.anthropic.asyncio.to_thread') as mock_to_thread:
        mock_to_thread.return_value = None  # simulate no files found
        with patch('app.services.collectors.anthropic.settings') as mock_settings:
            mock_settings.CLAUDE_PRO_LIMIT = 2000000
            mock_settings.CLAUDE_FREE_LIMIT = 500000
            mock_settings.CLAUDE_PROJECTS_DIR = ""

            result = await collector._get_claude_local_enhanced()

    mock_to_thread.assert_called_once()
    called_fn = mock_to_thread.call_args[0][0]
    assert callable(called_fn), "asyncio.to_thread must be called with a callable"
```

- [ ] **Step 3: Run test to verify it fails**

```bash
pytest tests/unit/test_collectors.py::TestAnthropicCollector::test_get_claude_local_enhanced_uses_to_thread -v
```

Expected: FAIL — `mock_to_thread.assert_called_once()` fails because `asyncio.to_thread` is never called.

- [ ] **Step 4: Refactor _get_claude_local_enhanced to use asyncio.to_thread**

In `app/services/collectors/anthropic.py`:

**a) Add `import asyncio` at the top of the file if not present** (check for existing import first with grep).

**b) Replace the entire `_get_claude_local_enhanced` method** (lines 722–854) with:

```python
    async def _get_claude_local_enhanced(self) -> List[Dict[str, Any]]:
        """
        Enhanced fallback: Parse Claude usage from local project logs.
        Offloads blocking file I/O to a thread to avoid blocking the event loop.
        """
        return await asyncio.to_thread(self._get_claude_local_enhanced_sync)

    def _get_claude_local_enhanced_sync(self) -> List[Dict[str, Any]]:
        """
        Synchronous implementation of local log parsing.
        Called via asyncio.to_thread — must not be awaited directly.

        Scans multiple config directories for .jsonl files and tracks all
        token types including cache reads and cache creation.

        Features:
        - Multiple config roots (CLAUDE_CONFIG_DIR comma-separated)
        - All token types: input, cache_read, cache_creation, output
        - Deduplication by message.id + requestId
        - 5-hour sliding window to match OAuth behavior

        Data Source:
        - Locations: CLAUDE_CONFIG_DIR or defaults (~/.claude/projects, ~/.config/claude/projects)
        - Format: JSONL with entries containing usage field

        Returns:
            List[Dict[str, Any]]: Single card with total tokens or None if logs unavailable
        """
        # Get config directories to scan
        config_dirs = self._get_config_dirs()

        # Find all .jsonl files across all config directories
        all_files = []
        for projects_dir in config_dirs:
            files = glob.glob(f"{projects_dir}/**/*.jsonl", recursive=True)
            all_files.extend(files)

        if not all_files:
            logger.debug(f"No Claude project log files found in any config directory")
            return None

        # Read credentials file for tier info
        tier = None
        try:
            if os.path.exists(self._credentials_path):
                with open(self._credentials_path, "r") as f:
                    data = json.load(f)
                    plan = data.get("account", {}).get("plan", "").lower()
                    if plan:
                        tier = plan.capitalize()
        except Exception as e:
            logger.debug(f"Could not read tier from credentials: {e}")

        # 5-hour window to match OAuth session window
        # Default to pro limit if we can't determine tier (safer assumption for limits)
        limit = settings.CLAUDE_FREE_LIMIT if tier == "Free" else settings.CLAUDE_PRO_LIMIT
        cutoff = datetime.now(timezone.utc) - timedelta(hours=5)

        # Track tokens and deduplicate
        total_tokens = 0
        seen_messages = set()  # For deduplication: (message_id, request_id)
        oldest: Optional[datetime] = None

        for fpath in all_files:
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        # Only process assistant messages with usage
                        if entry.get("type") != "assistant":
                            continue

                        # Parse timestamp
                        ts_raw = entry.get("timestamp")
                        if not ts_raw:
                            continue

                        try:
                            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                        except ValueError:
                            continue

                        if ts < cutoff:
                            continue

                        # Deduplicate by message.id + requestId
                        msg_data = entry.get("message", {})
                        msg_id = msg_data.get("id", "")
                        request_id = msg_data.get("requestId", "")
                        dedup_key = (msg_id, request_id)

                        if dedup_key in seen_messages:
                            continue
                        seen_messages.add(dedup_key)

                        # Sum all token types
                        usage = msg_data.get("usage", {})
                        input_tokens = usage.get("input_tokens", 0)
                        output_tokens = usage.get("output_tokens", 0)
                        cache_read = usage.get("cache_read_tokens", 0)
                        cache_creation = usage.get("cache_creation_tokens", 0)

                        total_tokens += input_tokens + output_tokens + cache_read + cache_creation

                        if not oldest or ts < oldest:
                            oldest = ts

            except FileNotFoundError:
                continue
            except Exception as e:
                logger.warning(f"Error reading Claude log file {fpath}: {e}")
                continue

        # Calculate remaining and percentage
        remaining = max(0, limit - total_tokens)
        pct = (total_tokens / limit * 100) if limit > 0 else 0
        reset_at = (oldest + timedelta(hours=5)) if oldest else None

        return [{
            "service": "Claude Pro",
            "icon": "🟠",
            "remaining": f"{remaining:,}",
            "unit": "tokens / 5h",
            "reset": human_delta(reset_at),
            "health": "good" if pct < 70 else "warning" if pct < 90 else "critical",
            "pace": PaceCalculator.estimate_longevity(pct, reset_at),
            "detail": f"{total_tokens:,} / {limit:,} [Local Logs] | cli-local",
            "used_value": float(total_tokens),
            "limit_value": float(limit),
            "is_unlimited": False,
            "tier": tier,
            "unit_type": "tokens",
            "reset_at": reset_at.isoformat() if reset_at else None,
            "data_source": "local",
            "usage_url": "https://claude.ai/settings/usage",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }]
```

- [ ] **Step 5: Verify asyncio is imported in anthropic.py**

```bash
grep -n "^import asyncio" /home/bjoern/projects/ai-usage-tracker/app/services/collectors/anthropic.py
```

If not found, add `import asyncio` at the top of the file alongside the other stdlib imports.

- [ ] **Step 6: Run the new test to verify it passes**

```bash
pytest tests/unit/test_collectors.py::TestAnthropicCollector::test_get_claude_local_enhanced_uses_to_thread -v
```

Expected: PASS

- [ ] **Step 7: Run full test suite**

```bash
pytest tests/ -v --tb=short 2>&1 | tail -30
```

Expected: all previously-passing tests still pass.

- [ ] **Step 8: Commit**

```bash
git add app/services/collectors/anthropic.py tests/unit/test_collectors.py
git commit -m "fix(anthropic): offload blocking file I/O in _get_claude_local_enhanced to asyncio.to_thread"
```

---

## Verification

After all tasks are complete, run the full suite and start the server to manually verify:

```bash
# Full test suite
pytest tests/ -v 2>&1 | tail -40

# Start server and hit the limits endpoint
uvicorn app.main:app --reload --port 8765 &
sleep 2
curl -s http://localhost:8765/api/limits | python3 -m json.tool | head -40
```

Expected: all tests pass, dashboard loads without errors, Claude card shows data from the correct source.
