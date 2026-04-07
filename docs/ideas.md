# Future Ideas & Improvements

This document tracks planned enhancements for Runway, organized by category and priority.

---

## Frontend

### High Priority

#### 1. Frontend Error Messages Enhancement
**File**: `frontend/js/app.js:49-66`  
**Severity**: High  
**Effort**: 1-2 hours

**Current Issue**:
- Generic catch block doesn't log error details
- Error banner doesn't show error message to user
- No way to distinguish between network, server, or parsing errors

**Suggested Implementation**:
```javascript
} catch (err) {
    console.error('Failed to fetch limits:', err);
    const errorMsg = err.message || 'Unknown error';
    errorBanner.textContent = `> Error: ${errorMsg}`;
    errorBanner.classList.remove('hidden');
    
    // Log error type for debugging
    if (err instanceof TypeError) {
        console.debug('Network error detected');
    } else if (err instanceof SyntaxError) {
        console.debug('JSON parsing error detected');
    }
}
```

**Benefits**:
- Better user experience (see actual errors)
- Easier debugging for users reporting issues
- Can identify patterns in failures

---

#### 2. Frontend Type Safety (JSDoc Annotations)
**Files**: `frontend/js/*.js`  
**Severity**: High  
**Effort**: 3-4 hours

**Current Issue**: Pure JavaScript with no type hints

**Suggested Implementation**:
```javascript
/**
 * Fetch all limits from backend
 * @returns {Promise<{limits: Array<LimitCard>}>}
 */
export async function fetchLimits() { ... }

/**
 * Render a single limit card
 * @param {LimitCard} card - The card data to render
 * @returns {HTMLElement} The rendered DOM element
 */
function renderCard(card) { ... }

/**
 * @typedef {Object} LimitCard
 * @property {string} service - Service name
 * @property {string} icon - Emoji icon
 * @property {string} remaining - Remaining capacity
 * @property {string} unit - Unit of measurement
 * @property {string} reset - Human-readable reset time
 * @property {string} health - "good" | "warning" | "critical"
 * @property {string} pace - Burn rate descriptor
 * @property {string} detail - Additional details
 */
```

**Benefits**:
- IDE autocompletion in VSCode
- Catches type errors early
- Better documentation for contributors
- No build step required (native JS)

---

### Medium Priority

#### 3. Dashboard Auto-Refresh UI Toggle
**File**: `frontend/index.html` + `frontend/js/app.js`  
**Severity**: Medium  
**Effort**: 2-3 hours

**Current Issue**: Dashboard doesn't auto-refresh, static view

**Suggested Implementation**:
```javascript
class DashboardManager {
    constructor() {
        this.refreshInterval = null;
        this.autoRefreshEnabled = localStorage.getItem('autoRefresh') === 'true';
        this.refreshRate = parseInt(localStorage.getItem('refreshRate')) || 60000; // 60s default
    }
    
    startAutoRefresh() {
        if (this.refreshInterval) return;
        
        this.refreshInterval = setInterval(() => {
            this.fetchAndRender();
        }, this.refreshRate);
    }
    
    stopAutoRefresh() {
        if (this.refreshInterval) {
            clearInterval(this.refreshInterval);
            this.refreshInterval = null;
        }
    }
}
```

**HTML Changes**:
```html
<div class="controls">
    <label>
        <input type="checkbox" id="autoRefresh" /> 
        Auto-refresh
    </label>
    <select id="refreshRate">
        <option value="30000">30s</option>
        <option value="60000" selected>60s</option>
        <option value="300000">5m</option>
    </select>
</div>
```

**Benefits**:
- Real-time feel without constant fetching
- Configurable refresh rates
- Saves to localStorage for persistence
- Respects user preferences

---

## Collectors

### High Priority

#### 1. Claude OAuth Token Refreshing
**File**: `app/services/collectors/anthropic.py`  
**Severity**: High  
**Effort**: 4-6 hours

Implement automatic token refreshing via `refreshToken` in `~/.claude/.credentials.json` if the primary `accessToken` is expired (typically expires after a few hours/days). 

**Note**: This would require writing back to the credentials file, which needs careful handling of file permissions and potential race conditions.

---

#### 2. GitHub OAuth Device Flow
**File**: `app/services/collectors/github.py` + frontend  
**Severity**: High  
**Effort**: 6-8 hours

Replace manual `GITHUB_TOKEN` entry with the official [Device Flow](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps#device-flow). 
- Display user code in frontend.
- Poll for access token in background.
- Particularly useful for headless/Docker environments where browser redirects are difficult.

**Related**: Inspired by `CodexBar` - see `docs/competitors.md` for reference.

---

#### 3. ChatGPT Web Dashboard Scraping
**File**: `app/services/collectors/chatgpt.py` (new)  
**Severity**: High  
**Effort**: 1-2 days

Implement optional scraping of `https://chatgpt.com/codex/settings/usage` to get rate limits, credits, and detailed usage charts.
- Support manual `Cookie:` header input for headless environments.
- Support automatic cookie extraction from Safari/Chrome/Firefox on macOS (experimental).
- *Inspiration: CodexBar's web scraping path.*

---

### Medium Priority

#### 4. Multi-Browser Cookie Support (All Collectors)
**Files**: `app/core/chrome_cookies.py`, `app/services/collectors/*.py`  
**Severity**: Medium  
**Effort**: 4-6 hours

Currently only Chrome is supported for automatic cookie extraction across all collectors (Claude, OpenCode). Add support for:

**Firefox** (`~/.mozilla/firefox/*/cookies.sqlite`):
- SQLite database format
- May be encrypted (requires NSS library on some platforms)

**Safari** (`~/Library/Cookies/Cookies.binarycookies`):
- Binary plist format
- Different encryption than Chrome
- macOS only

**Edge** (Chromium-based):
- Similar to Chrome, different profile paths
- `~/.config/microsoft-edge/Default/Cookies` on Linux
- `~/Library/Application Support/Microsoft Edge/Default/Cookies` on macOS

**Priority**: Low (Chrome covers 80%+ of users)

**Related**: See `docs/collectors/claude.md` and `docs/collectors/opencode.md` for current Chrome-only implementations.

---

#### 5. Missing Docstrings in Collectors
**Files**: `app/services/collectors/*.py`  
**Severity**: Medium  
**Effort**: 2-3 hours

**Current Gap**: No docstrings explaining collection strategy

**Suggested Pattern**:
```python
class AnthropicCollector(BaseCollector):
    """
    Collects Claude Pro usage limits using a 3-tier strategy.
    
    Strategy:
    1. Try OAuth API if CLAUDE_CODE_OAUTH_TOKEN is available
       - Fetches real-time usage from Anthropic's OAuth endpoint
       - Returns multiple quotas (5h, 7d windows)
    2. Fallback to local log parsing (~/.claude/projects)
       - Scans .jsonl files from last 5 hours
       - Counts input/output tokens manually
    3. Return error card if both fail
    
    Caching:
    - OAuth results cached for 10 minutes to avoid rate limits
    - Local logs read fresh on every request
    
    Raises:
    - Returns error cards instead of raising exceptions
    """
    
    async def collect(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """
        Collect Claude usage limits.
        
        Args:
            client: AsyncHTTP client for making requests
        
        Returns:
            List of limit card dictionaries or error card
        """
```

**Benefits**:
- Onboarding new contributors becomes easier
- Helps future maintainers understand design decisions
- Can be extracted for API docs

---

## Architecture & Backend

### High Priority

#### 1. Add Unit Tests & Integration Tests
**Directory**: `tests/`  
**Severity**: High  
**Effort**: 1-2 days

**Current Gap**: Zero test coverage

**Suggested Structure**:
```
tests/
├── unit/
│   ├── test_collectors.py          # Test each collector in isolation
│   ├── test_config.py              # Test configuration loading
│   ├── test_utils.py               # Test PaceCalculator, retry logic, etc.
│   └── test_schemas.py             # Test Pydantic models
├── integration/
│   ├── test_endpoints.py           # Test API endpoints
│   ├── test_collector_manager.py   # Test orchestration
│   └── conftest.py                 # Shared fixtures
└── fixtures/
    └── mock_responses.json         # Mock API responses
```

**Key Tests to Add**:
- Collector error scenarios (API down, invalid tokens, malformed data)
- Timeout handling (verify collectors fail gracefully)
- Rate limit retry logic (verify exponential backoff)
- External metrics ingestion
- Concurrent collector execution

**Testing Framework**: `pytest` + `pytest-asyncio` for async tests

---

### Medium Priority

#### 2. Response Caching Strategy
**File**: `app/services/collector_manager.py`  
**Severity**: Medium  
**Effort**: 4-6 hours

**Current Issue**: All collectors run on every request to `/api/limits`

**Suggested Implementation**:
```python
class CollectorManager:
    def __init__(self):
        self.collectors = [...]
        self._cache = {}
        self._cache_times = {}
        self._cache_ttl = {
            'anthropic': 600,      # 10 min (OAuth rate limit safety)
            'gemini': 300,         # 5 min (frequent resets)
            'github': 900,         # 15 min (stable)
            'opencode': 1800,      # 30 min (rarely changes)
            # ...
        }
    
    async def collect_all(self) -> List[Dict[str, Any]]:
        """Collect with per-collector TTL caching."""
        results = {}
        now = time.time()
        
        for name, collector in zip(collector_names, self.collectors):
            if name in self._cache:
                age = now - self._cache_times[name]
                if age < self._cache_ttl.get(name, 300):
                    results[name] = self._cache[name]
                    continue
            
            # Collect fresh data
            data = await collector.collect(client)
            self._cache[name] = data
            self._cache_times[name] = now
            results[name] = data
```

**Benefits**:
- Reduced API calls during heavy dashboard usage
- Respects provider rate limits better
- Frontend can show "cached X seconds ago" badge
- Faster dashboard load times

---

#### 3. Implement Strategy Pattern for Collectors
**Files**: `app/services/collectors/*.py`  
**Severity**: Medium  
**Effort**: 2-3 hours

All collectors inherit from `BaseCollector` but could benefit from consistent interface:

```python
class BaseCollector(ABC):
    """Base class with consistent strategy pattern."""
    
    @abstractmethod
    async def _primary_strategy(self) -> Optional[List[Dict]]:
        """Try primary data source (API)."""
        pass
    
    @abstractmethod
    async def _fallback_strategy(self) -> Optional[List[Dict]]:
        """Try fallback source (logs)."""
        pass
    
    @abstractmethod
    async def _error_handler(self, error: Exception) -> List[Dict]:
        """Return appropriate error card."""
        pass
```

---

#### 4. Error Card Categorization
**File**: `app/core/utils.py`  
**Severity**: Medium  
**Effort**: 2-3 hours

**Current Issue**: All errors look the same, hard to diagnose

**Suggested Implementation**:
```python
def error_card(service: str, icon: str, message: str, error_type: str = "unknown"):
    """
    Create an error card with categorized error types.
    
    error_type options:
    - "missing_config": Missing .env variable or credential file
    - "auth_failed": Invalid token or authentication issue
    - "rate_limited": API rate limit (429)
    - "timeout": Request timed out
    - "parse_error": Invalid response format
    - "api_error": Generic API error
    - "unknown": Unknown error
    """
    error_colors = {
        "missing_config": "🟡",  # Yellow
        "auth_failed": "🔴",      # Red
        "rate_limited": "🟠",     # Orange
        "timeout": "⏱️",          # Timeout symbol
        "parse_error": "⚠️",      # Warning
        "api_error": "❌",         # Error
        "unknown": "❓",          # Question mark
    }
    
    return {
        "service": service,
        "icon": error_colors.get(error_type, icon),
        "remaining": "ERR",
        "error_type": error_type,  # New field for frontend
        "detail": message,
        "health": "critical",
    }
```

**Benefits**:
- Frontend can style different error types differently
- Easier to spot patterns ("most failures are auth")
- Users can self-diagnose issues

---

#### 5. Move Away from Hardcoded Limits
**Files**: `app/services/collectors/*.py`  
**Severity**: Medium  
**Effort**: 1-2 days

**Current State**: Claude limit hardcoded to 2,000,000 tokens

**Suggested Approach**:
- Query local IDE config files for plan information
- For Anthropic: check `~/.claude/.credentials.json` for subscription tier
- For Gemini: Use tier detection API endpoint
- Store limits in config, not in code

**Note**: This is the biggest win from API-first approaches (see `docs/competitors.md`).

---

### Low Priority

#### 6. Smart Differential Fetching
**File**: `app/services/collectors/*.py`  
**Severity**: Low  
**Effort**: 2-3 days

**Current Issue**: Collectors run every time, even if nothing changed

**Suggested Pattern**:
```python
class SmartCollector(BaseCollector):
    """Only fetch if last result is stale or errored."""
    
    def __init__(self):
        self.last_result = None
        self.last_error_count = 0
        self.error_threshold = 3  # Retry after 3 errors
    
    async def collect(self, client: httpx.AsyncClient):
        # If last collection was recent and successful, skip
        if self.should_use_cache():
            return self.last_result
        
        # Otherwise fetch fresh
        try:
            result = await self._fetch_fresh(client)
            self.last_error_count = 0
            self.last_result = result
            return result
        except Exception as e:
            self.last_error_count += 1
            # Return stale result if we have one, else error card
            return self.last_result or error_card(...)
```

**Benefits**:
- Fewer API calls overall
- Graceful degradation on failures
- Reduced latency for end users

---

#### 7. Lazy Load Collectors
**File**: `app/services/collector_manager.py`  
**Severity**: Low  
**Effort**: 2-3 hours

Currently all collectors instantiate on startup. Could lazy-load only requested ones based on configuration.

---

#### 8. Concurrent Collector Timeout Protection
**File**: `app/services/collector_manager.py`  
**Severity**: Low  
**Effort**: 2-3 hours

Add global timeout across all collectors (not just individual ones):

```python
async def collect_all_with_timeout(self, timeout: float = 30.0):
    """Collect with overall timeout."""
    try:
        return await asyncio.wait_for(
            asyncio.gather(...),
            timeout=timeout
        )
    except asyncio.TimeoutError:
        # Return partial results + error cards for timed-out collectors
```

---

## Sidecar & Ingestion

### Medium Priority

#### 1. Auto-Updating Sidecar
**File**: `sidecar/`  
**Severity**: Medium  
**Effort**: 4-6 hours

Enable the sidecar to self-update by checking against the main Runway server's version or a remote Git repository.

---

#### 2. Daemon Mode
**File**: `sidecar/sidecar.py`  
**Severity**: Medium  
**Effort**: 2-3 hours

Support a `--daemon` flag to run as a persistent process with a configurable sleep interval, providing more real-time updates than 30m crontab tasks.

---

#### 3. Offline Queuing
**File**: `sidecar/sidecar.py`  
**Severity**: Medium  
**Effort**: 4-6 hours

If the ingestion API is unreachable, cache collected metrics in a local SQLite/JSON file and retry upon the next successful connection.

---

#### 4. Binary Sidecar Distribution
**File**: `sidecar/` (build scripts)  
**Severity**: Medium  
**Effort**: 1-2 days

Distribute the sidecar as a single-binary (using PyInstaller or Go) to avoid Python dependency issues on host machines.

---

## Documentation

### Low Priority

#### 1. Architecture Decision Records (ADRs)
**File**: `docs/adr/`  
**Severity**: Low  
**Effort**: 1 day

Document key decisions:
- Why we chose local-first over centralized API
- Why stateless (no database)
- Why specific collector fallback strategies
- Why environment-based credentials

---

#### 2. Troubleshooting Guide
**File**: `docs/TROUBLESHOOTING.md`  
**Severity**: Low  
**Effort**: 2-3 hours

Guide for common issues:
- "Why am I getting 'ERR' for Claude?"
- "How do I update expired tokens?"
- "Why aren't my logs being recognized?"
- "What does '[Cached]' mean?"

---

## Future Ideas (Low Priority / Research)

These are interesting concepts but require significant architectural changes or violate current design principles.

### 1. WebSocket Plugin Sync
**Inspiration**: cockpit-tools (see `docs/competitors.md`)

Use a local WebSocket (port `19528`) to talk to a browser extension. When a user logs into a web-based AI (like Claude or ChatGPT), the extension "sniffs" the token and pushes it to the local app.

**Complexity**: High - requires browser extension development
**Usefulness**: Medium - reduces manual token entry

---

### 2. Historical Tracking & Burndown Charts
**File**: `app/services/` (new module)  
**Severity**: Low  
**Effort**: 1-2 days  
**Concern**: Violates stateless principle

**Suggested Data Model**:
```python
class HistoricalMetrics:
    """Track usage over time for trend analysis."""
    
    def __init__(self):
        self.db_path = "~/.usage-tracker/history.db"  # SQLite
        self.init_db()
    
    async def record(self, limits: List[Dict]):
        """Store snapshot of all limits."""
        snapshot = {
            "timestamp": datetime.now(),
            "limits": limits,
        }
        self.db.insert("snapshots", snapshot)
```

**Frontend Enhancements**:
- Line chart showing usage trend over last 7 days
- Estimated burndown rate
- ETA for quota exhaustion
- Comparison with last week

**Benefits**:
- Identify usage patterns
- Predict when quota will run out
- Track optimization improvements

**Trade-offs**:
- Requires persistent storage (violates stateless design)
- Increases complexity significantly
- Could be done by external tool ingesting from `/api/limits`

---

### 3. Metrics Export Formats
**File**: `app/api/routes.py`  
**Severity**: Low  
**Effort**: 3-4 hours

**Current Issue**: API only returns JSON

**Suggested Enhancement**:
```python
@app.get("/api/limits")
async def get_limits(format: str = "json"):
    """
    Get all limits in requested format.
    
    Formats:
    - json (default)
    - csv (for Excel/spreadsheet import)
    - prometheus (for monitoring systems)
    - html (human-readable table)
    """
    limits = await manager.collect_all()
    
    if format == "csv":
        return StreamingResponse(export_csv(limits), 
                               media_type="text/csv")
    elif format == "prometheus":
        return Response(export_prometheus_metrics(limits),
                       media_type="text/plain")
    elif format == "html":
        return HTMLResponse(export_html_table(limits))
    else:
        return {"limits": limits}
```

**Benefits**:
- Integration with monitoring systems (Prometheus, Grafana)
- Can import into spreadsheets
- Opens up for analytics/BI tools

**Trade-offs**:
- Additional maintenance burden
- Not core to tracking functionality

---

### 4. Webhook Notifications for Threshold Alerts
**File**: `app/services/` (new module)  
**Severity**: Low  
**Effort**: 4-6 hours

**Suggested Pattern**:
```python
class AlertManager:
    """Send alerts when quotas cross thresholds."""
    
    async def check_and_alert(self, limits: List[Dict]):
        """
        Check if any limits crossed configured thresholds.
        
        Thresholds (configurable):
        - Critical: >90% used
        - Warning: >70% used
        - Info: >50% used
        """
        webhooks = config.ALERT_WEBHOOKS  # Discord, Slack URLs
        
        for limit in limits:
            pct = parse_percent(limit['remaining'])
            
            if pct > 90:
                await self.notify("critical", limit, webhooks)
            elif pct > 70:
                await self.notify("warning", limit, webhooks)
```

**Webhook Payload**:
```json
{
  "service": "Claude Pro",
  "status": "critical",
  "remaining": "5%",
  "reset": "in 2h 30m",
  "timestamp": "2026-04-07T12:45:00Z"
}
```

**Benefits**:
- Get notified before running out of quota
- Integrates with Discord/Slack teams
- Prevents "out of quota" surprises

**Trade-offs**:
- Adds background task complexity
- Requires configuration management
- Not critical for core functionality

---

*Last updated: 2026-04-07*
