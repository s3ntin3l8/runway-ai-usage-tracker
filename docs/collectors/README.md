# Provider collectors

One document per provider. Every provider in
[`app/core/registry.json`](../../app/core/registry.json) is listed here, with
the module that implements it.

Start with the [provider map](#provider-map) below, then read the provider's
own document.

## Provider map

| Registry id | Display name | Collection | Credential | Server collector | Sidecar events (`scripts/sidecar_pkg/`) | Document |
|---|---|---|---|---|---|---|
| `anthropic` | Claude Pro | OAuth → web → desktop credentials | `CLAUDE_CODE_OAUTH_TOKEN` (optional) | `anthropic.py` | `event_extractors/anthropic.py`, `cowork_events.py` | [claude.md](claude.md) |
| `anthropic_console` | Claude Console | Web session (platform.claude.com) | session cookie | `anthropic_console.py` | — | [claude.md](claude.md) |
| `antigravity` | Antigravity | Cloud Code Assist API | agy OAuth token (detected) | `antigravity.py` | `event_extractors/antigravity.py` | [antigravity.md](antigravity.md) |
| `chatgpt` | ChatGPT Codex | OAuth → web session | `CHATGPT_OAUTH_TOKEN` (optional) | `chatgpt.py` | `event_extractors/chatgpt.py` | [chatgpt.md](chatgpt.md) |
| `deepseek` | DeepSeek | REST (balance) | `DEEPSEEK_API_KEY` | `deepseek.py` | — | [deepseek.md](deepseek.md) |
| `gemini` | Gemini API | OAuth API | `GEMINI_OAUTH_*` (optional) | `gemini.py` | `event_extractors/gemini.py` | [gemini.md](gemini.md) |
| `github` | GitHub API | REST | `GITHUB_TOKEN` | `github.py` | — | [github.md](github.md) |
| `kimi_api` | Kimi API | REST (balance) | `KIMI_API_KEY` | `kimi_api.py` | — | [kimi_api.md](kimi_api.md) |
| `kimi_coding` | Kimi Coding | REST (IDE quotas) | `KIMI_CODE_API_KEY` or CLI login | `kimi_coding.py` | — | [kimi_coding.md](kimi_coding.md) |
| `kimi_k2` | Kimi K2 | REST (credits) | `KIMI_K2_API_KEY` | `kimi_k2.py` | — | [kimi_k2.md](kimi_k2.md) |
| `minimax` | MiniMax | REST (coding plan) | `MINIMAX_API_KEY` | `minimax.py` | — | [minimax.md](minimax.md) |
| `ollama` | Ollama Cloud | Web (cloud) session | `OLLAMA_SESSION_TOKEN` (optional) | `ollama.py` | — | [ollama.md](ollama.md) |
| `opencode` | OpenCode | Web session / sidecar | browser cookie (detected) | `opencode.py` | `event_extractors/opencode.py` | [opencode.md](opencode.md) |
| `openrouter` | OpenRouter | REST (credits) | `OPENROUTER_API_KEY` | `openrouter.py` | — | [openrouter.md](openrouter.md) |
| `xai` | xAI (Grok) | CLI chat proxy | `GROK_OAUTH_TOKEN` (optional) | `xai.py` | `event_extractors/xai.py` | [xai.md](xai.md) |
| `zai` | zAI API | REST (quota) | `ZAI_API_KEY` | `zai.py` | — | [zai.md](zai.md) |

All server collectors live in `app/services/collectors/` and share
`base.py` (the strategy loop, the enrichment merge and the card contract) and
`oauth_base.py` (shared OAuth token handling).

### Sidecar-only sources

Two sources are **not** server providers. They exist only as sidecar overlay
providers in `scripts/sidecar_registry_overlay.json`, because the server must
not list them as providers:

| Source | Sidecar module | Document |
|---|---|---|
| `hermes` | `scripts/sidecar_pkg/event_extractors/hermes.py` | [hermes.md](hermes.md) |
| `minimax_code` | `scripts/sidecar_pkg/event_extractors/minimax_code.py` (helpers in `minimax_code_schema.py`, `minimax_code_roots.py`, `minimax_code_ledger.py`) | [minimax_code.md](minimax_code.md) |

They produce **usage events**, never quota cards. The server receives their
events and files them under the canonical provider they map to.

### `data_source` and `input_source`

Every card carries both. They are defined once, in
[`../collection_logic.md`](../collection_logic.md#card-provenance-labels) —
the source of truth for their values; the other documents link to it rather
than restating them.

One note that is easy to get wrong: **`data_source: "local"` is never emitted
by a server collector.** The constant exists in `app/services/collectors/base.py`,
but no collector assigns it. A card with `local` reached the server because a
sidecar pushed it.

## Related

- [`../collection_logic.md`](../collection_logic.md) — how a collector turns a
  response into a card.
- [`../architecture.md`](../architecture.md) — the data model the cards land in.
- [`../api-reference.md`](../api-reference.md) — the `LimitCard` schema.
