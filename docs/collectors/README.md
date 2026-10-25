# Provider collectors

One document per provider. Every provider in
[`app/core/registry.json`](../../app/core/registry.json) is listed here, with
the module that implements it.

Start with the [provider map](#provider-map) below, then read the provider's
own document.

## Provider map

| Registry id | Display name | Server collector | Sidecar side |
|---|---|---|---|
| `anthropic` | Claude Pro | `anthropic.py` | `anthropic_oauth.py`, `anthropic_web.py` — see [claude.md](claude.md) |
| `anthropic_console` | Claude Console | `anthropic_console.py` | — see [claude.md](claude.md) |
| `antigravity` | Antigravity | `antigravity.py` | `antigravity_api.py`, `antigravity_oauth.py` — see [antigravity.md](antigravity.md) |
| `chatgpt` | ChatGPT Codex | `chatgpt.py` | `chatgpt_oauth.py`, `chatgpt_web.py` — see [chatgpt.md](chatgpt.md) |
| `deepseek` | DeepSeek | `deepseek.py` | — see [deepseek.md](deepseek.md) |
| `gemini` | Gemini API | `gemini.py` | `gemini_api.py`, `gemini_oauth.py` — see [gemini.md](gemini.md) |
| `github` | GitHub API | `github.py` | — see [github.md](github.md) |
| `kimi_api` | Kimi API | `kimi_api.py` | — see [kimi_api.md](kimi_api.md) |
| `kimi_coding` | Kimi Coding | `kimi_coding.py` | — see [kimi_coding.md](kimi_coding.md) |
| `kimi_k2` | Kimi K2 | `kimi_k2.py` | — see [kimi_k2.md](kimi_k2.md) |
| `minimax` | MiniMax | `minimax.py` | `minimax_code` event extractor — see [minimax.md](minimax.md) |
| `ollama` | Ollama Cloud | `ollama.py` | — see [ollama.md](ollama.md) |
| `opencode` | OpenCode | `opencode.py` | `opencode` event extractor — see [opencode.md](opencode.md) |
| `openrouter` | OpenRouter | `openrouter.py` | — see [openrouter.md](openrouter.md) |
| `xai` | xAI (Grok) | `xai.py` | `xai` event extractor — see [xai.md](xai.md) |
| `zai` | zAI API | `zai.py` | — see [zai.md](zai.md) |

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
| `minimax_code` | `scripts/sidecar_pkg/event_extractors/minimax_code.py` | [minimax.md](minimax.md) |

They produce **usage events**, never quota cards. The server receives their
events and files them under the canonical provider they map to.

### `data_source` and `input_source`

Every card carries both. They are defined once, in
[`../statistics.md`](../statistics.md) — that document is the source of truth
for their values; the other documents link to it rather than restating them.

One note that is easy to get wrong: **`data_source: "local"` is never emitted
by a server collector.** The constant exists in `app/services/collectors/base.py`,
but no collector assigns it. A card with `local` reached the server because a
sidecar pushed it.

## Documents

| Document | Provider |
|---|---|
| [antigravity.md](antigravity.md) | Antigravity |
| [chatgpt.md](chatgpt.md) | ChatGPT / Codex |
| [claude.md](claude.md) | Claude and Claude Console |
| [deepseek.md](deepseek.md) | DeepSeek |
| [gemini.md](gemini.md) | Gemini |
| [github.md](github.md) | GitHub Copilot |
| [hermes.md](hermes.md) | Hermes (sidecar-only) |
| [kimi_api.md](kimi_api.md) | Kimi API |
| [kimi_coding.md](kimi_coding.md) | Kimi Coding |
| [kimi_k2.md](kimi_k2.md) | Kimi K2 |
| [minimax.md](minimax.md) | MiniMax (and MiniMax Code, sidecar-only) |
| [ollama.md](ollama.md) | Ollama Cloud |
| [opencode.md](opencode.md) | OpenCode |
| [openrouter.md](openrouter.md) | OpenRouter |
| [xai.md](xai.md) | xAI (Grok) |
| [zai.md](zai.md) | zAI |

## Related

- [`../collection_logic.md`](../collection_logic.md) — how a collector turns a
  response into a card.
- [`../architecture.md`](../architecture.md) — the data model the cards land in.
- [`../api-reference.md`](../api-reference.md) — the `LimitCard` schema.
