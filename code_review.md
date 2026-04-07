# 🔍 Comprehensive Code Review: Runway (AI Usage Tracker)

Runway is a sophisticated, local-first monitoring tool designed to aggregate AI provider quotas and balances. This review covers the backend architecture, provider implementations, sidecar mechanism, and general code quality.

## 🏗️ Architecture & Design Pattern

### 1. Stateless & Local-First Philosophy
The project adheres strictly to a stateless, "no-database" design. It favors:
- **Direct Web API calls** using extracted browser cookies.
- **Log parsing** from local AI provider CLI tools (`~/.claude/`, `~/.gemini/`).
- **In-memory aggregation** with an ingestion API for secondary host data.

> [!NOTE]
> This architecture is highly unconventional but perfectly suited for its "Runway" niche—zero setup cost and high privacy. It leverages existing local states instead of creating new ones.

### 2. Collector Pattern & Resilience
The core of the application lies in `app/services/collectors/`. Each provider implements a four-tier fallback strategy:
1. **OAuth API**: Primary source for high-quality, real-time data.
2. **Web API (Cookie Scraping)**: Secondary source, bypassing official API limitations by using browser sessions.
3. **Local Log Parsing**: Tertiary source, providing "good enough" metrics when offline or unauthenticated.
4. **Graceful Error Cards**: Quaternary source, ensuring the UI remains functional even when collection fails.

### 3. Smart Differential Fetching
The `SmartCollector` wrapper in `app/services/smart_collector.py` is an architectural highlight. It handles:
- **TTL Caching**: Different providers have different refresh rates (e.g., Gemini: 5m, OpenCode: 30m).
- **Error Backoff**: Prevents hammering APIs during outages.
- **Graceful Degradation**: Automatically serves stale data with a `[Cached]` tag during failures.

---

## 💻 Code Quality & Implementation Analysis

### Backend (Python/FastAPI)
- **Typing**: Excellent use of Python 3.9+ type hints and Pydantic v2 models.
- **Async Concurrency**: The `CollectorManager.collect_all()` uses `asyncio.gather` for parallel fetching, significantly reducing dashboard load times.
- **Configuration**: `app/core/config.py` is robust, handling environment variables and searching for credentials in multiple OS-specific paths (Keychain, `~/.config`, etc.).

### Frontend (Tailwind/JS)
- **Rich Aesthetics**: The glassmorphism design (`styles.css`) is visually premium and modern.
- **UI Responsiveness**: The dashboard uses a CSS grid that transitions smoothly from mobile (1 column) to large displays (4 columns).
- **Performance**: Minimal dependencies (Tailwind CDN, Google Fonts) and plain JavaScript keep the frontend lightweight.

### Sidecar Mechanism (`scripts/sidecar.py`)
- **Standalone Design**: Built with zero external dependencies (no `httpx`, no `pydantic`), making it highly portable for secondary hosts.
- **Full Parity**: Replicates the complex collection logic of the main app (Chrom cookies, Log parsing).

---

## ⚖️ Strengths vs. Challenges

### ✅ Strengths
- **Incredible Fallback Robustness**: The ability to scrape Chrome cookies or parse local logs when API keys aren't set is a rare and powerful feature.
- **Premium UX**: High attention to design details (pulse animations for critical status, micro-interactions).
- **High Portability**: Docker support + Sidecar makes it usable across diverse environments.

### ⚠️ Potential Issues & Risks
- **Code Duplication**: `sidecar.py` replicates significant logic from the main collectors. Changes to provider APIs require updating both the main app and the sidecar.
- **Chrome Cookie Access**: Cookie extraction logic is platform-dependent and requires Chrome to be installed in specific paths. This is a potential point of failure for some users.
- **Security**: While stateless, the app reads sensitive browser cookies and credentials. Clearer documentation on how these are protected in non-localhost environments would be beneficial.

---

## 🚀 Recommendations for Improvement

### 1. Logic Unification (Advanced)
Consider extracting common collector logic into a lightweight shared package or a structured metadata file that both the sidecar and main collectors can consume. This would reduce the risk of logic drift.

### 2. Sidecar Authentication
Currently, the sidecar uses an `INGEST_API_KEY`. Adding support for a rotating secret or OIDC-based tokens would enhance security for Multi-Host deployments.

### 3. Dashboard Interactivity
- **Detail Drill-down**: Click on a card to see the raw logs or detailed usage breakdown.
- **Trend Visualization**: Add local-storage-based history for the last 24 hours to show usage curves.

### 4. Health Check Endpoint
Add a `/health` endpoint that returns the internal state of all collectors (Success vs. Failures) for monitoring purposes.

---

### Final Verdict: **Highly Impressive**
The **Runway** project is a masterclass in resilient collector engineering. Its "silent" data extraction capabilities (cookies & logs) set it apart from typical quota trackers, and the aesthetic execution is top-tier.


# Implementation Plan: Enhancing Runway & Security Documentation

This plan details the implementation of the four recommendations from the comprehensive code review, plus a specific example for documenting security in non-localhost environments.

## User Review Required

> [!IMPORTANT]
> **Sidecar Portability**: The proposed "Logic Unification" uses a `providers/registry.json`. While this reduces duplication, it means `sidecar.py` now depends on this file being present or bundled. I recommend a "Sidecar Generator" script to keep it zero-dependency while maintaining a single source of truth.

> [!WARNING]
> **Persistent Storage**: The "Trend Visualization" initially uses `localStorage`. For server/Docker deployments, this means data is client-side only. If you need historical trends shared across devices, we should consider a lightweight server-side storage (e.g., `aiosqlite`).

---

## 1. Logic Unification (Declarative Provider Registry)

### [NEW] [registry.json](file:///home/bjoern/projects/ai-usage-tracker/app/core/providers/registry.json)
Create a declarative registry for provider metadata to avoid logic drift between the main app and sidecar.

```json
{
  "anthropic": {
    "display": "Claude",
    "icon": "🟠",
    "endpoints": [
      {
        "url": "https://api.anthropic.com/api/oauth/usage",
        "method": "GET",
        "headers": { "anthropic-beta": "oauth-2025-04-20" },
        "auth_type": "bearer"
      }
    ]
  }
}
```

### [NEW] [generate_sidecar.py](file:///home/bjoern/projects/ai-usage-tracker/scripts/generate_sidecar.py)
A script that embeds the `registry.json` into a template of `sidecar.py`, ensuring the sidecar remains a single, portable file.

---

## 2. Secure Sidecar Authentication (HMAC Signing)

### [MODIFY] [ingest.py](file:///home/bjoern/projects/ai-usage-tracker/app/api/endpoints/ingest.py)
Shift from a static API key to HMAC-SHA256 signature verification to prevent replay attacks and token theft.

```python
import hmac, hashlib, time
from fastapi import Header

@router.post("/ingest")
async def ingest_metrics(
    request: IngestRequest,
    x_signature: str = Header(None),
    x_timestamp: str = Header(None)
):
    # Verify timestamp (within 5 minutes)
    if abs(time.time() - float(x_timestamp)) > 300:
        raise HTTPException(status_code=401, detail="Request expired")
    
    # Recreate signature
    expected_sigma = hmac.new(
        settings.INGEST_API_KEY.encode(),
        f"{x_timestamp}{request.model_dump_json()}".encode(),
        hashlib.sha256
    ).hexdigest()
    
    if not hmac.compare_digest(x_signature, expected_sigma):
        raise HTTPException(status_code=401, detail="Invalid signature")
    # ... process metrics
```

---

## 3. Dashboard Interactivity & Trends

### [MODIFY] [components.js](file:///home/bjoern/projects/ai-usage-tracker/frontend/js/components.js)
Implement SVG sparklines and a detail modal.

```javascript
// Detail Modal function
export function showDetailModal(item) {
    const modal = document.createElement('div');
    modal.className = 'fixed inset-0 bg-black/60 backdrop-blur-md z-50 flex items-center justify-center p-6';
    modal.innerHTML = `
        <div class="glass-panel max-w-lg w-full p-8 rounded-3xl">
            <h2 class="text-2xl font-bold">${item.icon} ${item.service}</h2>
            <p class="mt-4 text-zinc-400">${item.detail}</p>
            <button onclick="this.parentElement.parentElement.remove()" class="mt-8 px-6 py-2 bg-zinc-800 rounded-lg">Close</button>
        </div>
    `;
    document.body.appendChild(modal);
}
```

---

## 4. Health Check Endpoint

### [MODIFY] [routes.py](file:///home/bjoern/projects/ai-usage-tracker/app/api/routes.py)
Expose the internal state of the `SmartCollector` cache.

```python
@router.get("/health")
async def check_health():
    return {
        "status": "healthy",
        "collectors": manager.get_collector_stats()
    }
```

---

## 5. Security Example (Non-Localhost Environments)

### [NEW] [DOCS_SECURITY.md](file:///home/bjoern/projects/ai-usage-tracker/docs/SECURITY.md)
Provide clear guidance on how Runway protects data in Mode 3 (Docker/Server).

#### Example Documentation Snippet:
> **Security Architecture: Sidecar as a Proxy**
> In Dockerized environments, the main Runway app is isolated from your host's browser cookies. 
> 1. **Sidecar Ingestion**: Use the provided `sidecar.py` script on your local machine to extract tokens.
> 2. **Encrypted Tunnel**: Ensure `APP_HOST` is protected behind an HTTPS reverse proxy (e.g., Nginx with Let's Encrypt).
> 3. **HMAC Signing**: All sidecar data is signed with your `INGEST_API_KEY`. This ensures that even if your endpoint is public, only authenticated sidecars can push data.

---

## Verification Plan

### Automated Tests
- `pytest tests/unit/test_ingest_auth.py`: Verify HMAC signature verification.
- `pytest tests/integration/test_health_endpoint.py`: Verify health check output format.

### Manual Verification
- **Sidecar Smoke Test**: Run the generated sidecar with a dummy registry and verify it pushes data correctly.
- **UI Test**: Click a card on the dashboard to trigger the new Detail Modal.
- **Trend Test**: Refresh the page 5 times and verify the sparkline data persists in `localStorage`.
