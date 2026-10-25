# AI Usage Statistics Hierarchy & Terminology

Runway standardizes AI usage monitoring across disparate providers by using a unified taxonomy for **Data Sources** and **Input Sources**. This ensures transparency and consistency when tracking quotas across APIs, web scrapers, and local tools.

---

## Data Source (`data_source`)
The `data_source` label identifies **how** the usage data was obtained from the provider. Each collector uses a multi-tier fallback system.

| Label | Tier | Description | Typical Use Cases |
| :--- | :--- | :--- | :--- |
| **`api`** | Primary | Official/Public API endpoint or OAuth-authenticated service. | `anthropic_oauth`, `gemini_api`, `github`, `openrouter`, `kimi_coding` |
| **`web`** | Secondary | Web-mimicking calls or scraping using browser cookies. | `chatgpt` (wham/usage), `ollama` (scraping) |
| **`local`** | Tertiary | Reading local logs, CLI output, or local application databases. | Claude statusline quota windows and sidecar event extractors (Claude/Codex/Gemini/OpenCode JSONL/SQLite) |
| **`error`** | — | The card reports a failure instead of a quota. | A collector whose every strategy errored |
| **`lsp`** | — | A legacy value the ingest rule still recognises as the one exception to the sidecar-enrichment-only rule. **No shipped collector emits it.** | — |

> **`local` is never emitted by a server collector.** The constant exists in
> `app/services/collectors/base.py`, but no collector assigns it — a card with
> `local` reached the server because a **sidecar** pushed it.

### Fallback Logic
There is **no fixed `api` → `web` → `local` chain**. Each collector declares its
own ordered strategy list, and the **first primary strategy that does not
error wins** — the others are not tried. The declaration order *within one
collector* is the priority; there is no global ranking of `api` over `web` over
`local`.

Rate limiting is handled per collector, in `SmartCollector`: a `429` puts the
provider into a backoff state and the collector is skipped rather than
retried. It is not a generic "fall through to the next tier" rule.

### Enrichment Pattern
Some collectors support **enrichment**: strategies that run *in addition* to primary data and merge their results instead of replacing them.

**Use case**: API provides quota limits/remaining, local logs provide actual token usage breakdown.

**How it works**:
1. The strategies are partitioned by the presence of `{"enrich": True}` in
   their options.
2. The primary strategies run in declaration order; the first that does not
   error wins.
3. If a primary strategy succeeded, the enrichment strategies run *after* it.
4. `_enrich_results()` matches the enrichment to the primary card by
   `(service_name, variant, window_type, model_id)` — with fallback to less
   specific keys — and **overwrites** three fields: `token_usage`, `by_model`
   and `msgs`.
5. `pct_used` is then re-derived from the primary card's own `used_value` /
   `limit_value`, so enrichment can never change the quota percentage.

**Example**:
```
# Primary card:
{ "remaining": "25%", "used_value": 250000, "limit_value": 1000000,
  "token_usage": null, "by_model": null, "msgs": null }

# After enrichment:
{ "remaining": "25%", "used_value": 250000, "limit_value": 1000000,
  "token_usage": { "input": 125400, "output": 0 },
  "by_model": { "claude-sonnet-4": { "tokens": 125400 } },
  "msgs": 42 }
```

Enrichment is **not** appended to the `detail` string, and nothing is
maximised or summed — the enrichment values replace the card's own values for
those three fields.

> **No shipped collector declares an enrichment strategy.** The mechanism is
> implemented in the base class, but every collector in the registry uses only
> primary strategies. The per-message breakdowns reach the dashboard as
> **sidecar usage events**, not as a server-side enrichment strategy.
---

## Input Source (`input_source`)
The `input_source` label identifies **where** the credentials/data came from.

| Label | Description |
| :--- | :--- |
| **`config`** | Entered by the user directly into the Runway Settings UI (stored in DB). |
| **`server`** | Found by the server process itself (environment variables, `.env` file, or Runway's own `github_oauth.json`). CLI logins such as `~/.config/gh` are read by the sidecar, not the server. |
| **`sidecar`** | Forwarded from a remote sidecar (remote host logs, browser cookies, IDE/file introspection). |

---

## Example Usage
A single usage card emitted by a collector might look like this:

```json
{
  "service_name": "Claude",
  "variant": "Sonnet 4",
  "remaining": "85%",
  "data_source": "api",
  "input_source": "config",
  "updated_at": "2026-04-20T14:20:00Z"
}
```

`data_source` is `api` — obtained via the official OAuth API.
`input_source` is `config` — the token was pasted into Settings and is stored
in the database.

---

## Provider Dashboard Indicators

The dashboard renders the **raw values** — `data_source` and `input_source` —
not a friendly label. On the credential rows they are shown as the pair
joined by a slash, e.g. `api / config` or `web / sidecar`
(`webapp/src/features/settings/sections/credentials/display.ts`).

There is no `Source` column on the provider table and no tooltip that reads
`api (Official)`, `web (Unofficial)` or `local (Fast Path)` — those strings do
not exist anywhere in the frontend. The only "Source" UI is a **Filters**
popover on the provider page, where a `SidecarFilter` narrows the list by
sidecar.

What the values mean when you read them raw:

| Value | Reading |
|---|---|
| `api` | Official API or OAuth endpoint. The most reliable source, and the least likely to break. |
| `web` | Cookie-based or scraped web endpoint. More detail, but it breaks when the site's layout changes. |
| `local` | A local log, CLI statusline or fast-path cache. Lowest latency and works offline, but it can lag behind the server's own state. |
| `error` | The card reports a failure rather than a quota. |
| `lsp` | A legacy value no shipped collector emits. The ingest rule still recognises it as the one exception to the sidecar-enrichment-only rule. |
