# AI Usage Statistics Hierarchy & Terminology

Runway standardizes AI usage monitoring across disparate providers by using a unified taxonomy for **Data Sources** and **Input Sources**. This ensure transparency and consistency when tracking quotas across APIs, web scrapers, and local tools.

---

## Data Source (`data_source`)
The `data_source` label identifies **how** the usage data was obtained from the provider. Each collector uses a multi-tier fallback system.

| Label | Tier | Description | Typical Use Cases |
| :--- | :--- | :--- | :--- |
| **`api`** | Primary | Official/Public API endpoint or OAuth-authenticated service. | `anthropic_oauth`, `gemini_api`, `github`, `openrouter` |
| **`web`** | Secondary | Web-mimicking calls or scraping using browser cookies. | `chatgpt` (wham/usage), `ollama` (scraping), `kimi_coding` |
| **`local`** | Tertiary | Reading local logs, CLI output, or local application databases. | `claude_local` (statusline), `opencode_db`, sidecar event extractors (Claude/Codex/Gemini/OpenCode JSONL/SQLite) |

### Fallback Logic
Most providers follow an `api` → `web` → `local` fallback chain. This ensures the dashboard always shows the most accurate data available given the current authentication state.

### Enrichment Pattern
Some collectors support **enrichment**: strategies that run *in addition* to primary data and merge their results instead of replacing them.

**Use case**: API provides quota limits/remaining, local logs provide actual token usage breakdown.

**How it works**:
1. Primary strategy runs first (e.g., API)
2. If primary succeeds, enrichment strategies run *after*
3. Enrichment data is merged into the primary card's `detail` string

**Example**:
```
# Before (fallback):
API card: "25% remaining | 750K/1M tokens left"
Local not used → fallback runs only on API failure

# After (enrichment):
API card: "25% remaining | 750K/1M tokens left | Session: 125,400 tokens"
Local runs after API success, appending token usage to detail
```

---

## Input Source (`input_source`)
The `input_source` label identifies **where** the credentials/data came from.

| Label | Description |
| :--- | :--- |
| **`config`** | Entered by the user directly into the Runway Settings UI (stored in DB). |
| **`server`** | Discovered by the local server (Environment variables, `.env` file, or local config discovery like `~/.config/gh`). |
| **`sidecar`** | Forwarded from a remote sidecar (remote host logs, browser cookies, IDE/file introspection). |

---

## Example Usage
A single usage card emitted by a collector might look like this:

```json
{
  "service": "Claude 3.5 Sonnet",
  "remaining": "85%",
  "data_source": "api",      // Obtained via official OAuth API
  "input_source": "config",  // User pasted the token in settings (stored in DB)
  "updated_at": "2026-04-20T14:20:00Z"
}
```

---

## Provider Dashboard Indicators
In the Runway Dashboard, the **Source** column and the hovering tooltips display these types to help you debug authentication issues:

- **api (Official)**: Most reliable, least likely to break.
- **web (Unofficial)**: High detail but may break if the website layout changes.
- **local (Fast Path)**: Lowest latency, works offline, but may be lagging behind server-side state.