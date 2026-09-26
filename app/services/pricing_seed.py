"""Seed provider_pricing with current public rates.

Add new rows (don't modify existing) when prices change — the
effective_from column is the natural version key.
"""

from datetime import date

from sqlmodel import Session, select

from app.models.db import ProviderPricing

PRICING_SEED: list[dict] = [
    # Anthropic Claude (Fable 5, Sonnet 4.5, Opus 4.5, Haiku 4.5)
    {
        "provider_id": "anthropic",
        "model_id": "fable",
        "effective_from": "2025-09-01",
        "input_per_mtok": 10.00,
        "output_per_mtok": 50.00,
        "cache_read_per_mtok": 1.00,
        "cache_create_per_mtok": 12.50,  # 5m TTL writes: 1.25x input
        "cache_create_1h_per_mtok": 20.00,  # 1h TTL writes: 2x input
        "notes": "Fable 5",
    },
    {
        "provider_id": "anthropic",
        "model_id": "sonnet",
        "effective_from": "2025-09-01",
        "input_per_mtok": 3.00,
        "output_per_mtok": 15.00,
        "cache_read_per_mtok": 0.30,
        "cache_create_per_mtok": 3.75,  # 5m TTL writes: 1.25x input
        "cache_create_1h_per_mtok": 6.00,  # 1h TTL writes: 2x input
        "notes": "Sonnet 4.5",
    },
    {
        "provider_id": "anthropic",
        "model_id": "opus",
        "effective_from": "2025-09-01",
        "input_per_mtok": 5.00,
        "output_per_mtok": 25.00,
        "cache_read_per_mtok": 0.50,
        "cache_create_per_mtok": 6.25,  # 5m TTL writes: 1.25x input
        "cache_create_1h_per_mtok": 10.00,  # 1h TTL writes: 2x input
        "notes": "Opus 4.x effective rate",
    },
    {
        "provider_id": "anthropic",
        "model_id": "haiku",
        "effective_from": "2025-09-01",
        "input_per_mtok": 1.00,
        "output_per_mtok": 5.00,
        "cache_read_per_mtok": 0.10,
        "cache_create_per_mtok": 1.25,  # 5m TTL writes: 1.25x input
        "cache_create_1h_per_mtok": 2.00,  # 1h TTL writes: 2x input
        "notes": "Haiku 4.5 effective rate",
    },
    # OpenAI ChatGPT / Codex (GPT-5 series)
    {
        "provider_id": "chatgpt",
        "model_id": "gpt-5",
        "effective_from": "2025-08-01",
        "input_per_mtok": 1.25,
        "output_per_mtok": 10.00,
        "cache_read_per_mtok": 0.125,
        "cache_create_per_mtok": 0.0,
        "notes": "GPT-5 standard (rates per developers.openai.com/api/docs/pricing)",
    },
    {
        "provider_id": "chatgpt",
        "model_id": "codex",
        "effective_from": "2025-08-01",
        "input_per_mtok": 5.00,
        "output_per_mtok": 15.00,
        "cache_read_per_mtok": 1.25,
        "cache_create_per_mtok": 0.0,
        "notes": "GPT-5 Codex (Plus tier)",
    },
    # OpenAI ChatGPT — gpt-5.x series (rates per openai.com/api/pricing, 2026-05-01)
    {
        "provider_id": "chatgpt",
        "model_id": "gpt-5.5",
        "effective_from": "2026-05-01",
        "input_per_mtok": 5.00,
        "output_per_mtok": 30.00,
        "cache_read_per_mtok": 0.50,
        "cache_create_per_mtok": 0.0,
        "notes": "GPT-5.5 standard",
    },
    {
        "provider_id": "chatgpt",
        "model_id": "gpt-5.5-pro",
        "effective_from": "2026-05-01",
        "input_per_mtok": 30.00,
        "output_per_mtok": 180.00,
        "cache_read_per_mtok": 0.0,
        "cache_create_per_mtok": 0.0,
        "notes": "GPT-5.5 Pro",
    },
    {
        "provider_id": "chatgpt",
        "model_id": "gpt-5.4",
        "effective_from": "2026-05-01",
        "input_per_mtok": 2.50,
        "output_per_mtok": 15.00,
        "cache_read_per_mtok": 0.25,
        "cache_create_per_mtok": 0.0,
        "notes": "GPT-5.4 standard",
    },
    {
        "provider_id": "chatgpt",
        "model_id": "gpt-5.4-mini",
        "effective_from": "2026-05-01",
        "input_per_mtok": 0.75,
        "output_per_mtok": 4.50,
        "cache_read_per_mtok": 0.075,
        "cache_create_per_mtok": 0.0,
        "notes": "GPT-5.4 Mini",
    },
    {
        "provider_id": "chatgpt",
        "model_id": "gpt-5.4-nano",
        "effective_from": "2026-05-01",
        "input_per_mtok": 0.20,
        "output_per_mtok": 1.25,
        "cache_read_per_mtok": 0.02,
        "cache_create_per_mtok": 0.0,
        "notes": "GPT-5.4 Nano",
    },
    {
        "provider_id": "chatgpt",
        "model_id": "gpt-5.4-pro",
        "effective_from": "2026-05-01",
        "input_per_mtok": 30.00,
        "output_per_mtok": 180.00,
        "cache_read_per_mtok": 0.0,
        "cache_create_per_mtok": 0.0,
        "notes": "GPT-5.4 Pro",
    },
    {
        "provider_id": "chatgpt",
        "model_id": "codex",
        "effective_from": "2026-05-01",
        "input_per_mtok": 1.75,
        "output_per_mtok": 14.00,
        "cache_read_per_mtok": 0.175,
        "cache_create_per_mtok": 0.0,
        "notes": "gpt-5.3-codex (Standard)",
    },
    # OpenAI ChatGPT — gpt-5.6 codenamed generation (rates per
    # developers.openai.com/api/docs/pricing, checked 2026-09-09). Runway now
    # preserves the full slug (see _normalize_chatgpt_model) instead of
    # collapsing codenames to their bare version, so each needs its own row.
    {
        "provider_id": "chatgpt",
        "model_id": "gpt-6-astra",
        "effective_from": "2026-09-01",
        "input_per_mtok": 10.00,
        "output_per_mtok": 50.00,
        "cache_read_per_mtok": 1.00,
        "cache_create_per_mtok": 0.0,
        "notes": "GPT-6 Astra",
    },
    {
        "provider_id": "chatgpt",
        "model_id": "gpt-5.6-sol",
        "effective_from": "2026-09-01",
        "input_per_mtok": 4.00,
        "output_per_mtok": 20.00,
        "cache_read_per_mtok": 0.40,
        "cache_create_per_mtok": 0.0,
        "notes": (
            "GPT-5.6 Sol — promotional pricing, published as valid at least "
            "through 2026-11-21; recheck and version a new effective_from row "
            "after that date"
        ),
    },
    {
        "provider_id": "chatgpt",
        "model_id": "gpt-5.6-terra",
        "effective_from": "2026-09-01",
        "input_per_mtok": 2.00,
        "output_per_mtok": 12.00,
        "cache_read_per_mtok": 0.20,
        "cache_create_per_mtok": 0.0,
        "notes": "GPT-5.6 Terra",
    },
    {
        "provider_id": "chatgpt",
        "model_id": "gpt-5.6-luna",
        "effective_from": "2026-09-01",
        "input_per_mtok": 0.20,
        "output_per_mtok": 1.20,
        "cache_read_per_mtok": 0.02,
        "cache_create_per_mtok": 0.0,
        "notes": "GPT-5.6 Luna",
    },
    # OpenAI ChatGPT — gpt-5.4 / gpt-5.4-mini backdated to 2025-08-01. The
    # 2026-05-01 rows above (same rates) postdate this generation's actual
    # usage window (observed events run 2026-04-04 through 2026-04-28), so
    # without this row every gpt-5.4/-mini event before May 1st priced at
    # $0 — roughly half of all chatgpt events at the time this was found.
    {
        "provider_id": "chatgpt",
        "model_id": "gpt-5.4",
        "effective_from": "2025-08-01",
        "input_per_mtok": 2.50,
        "output_per_mtok": 15.00,
        "cache_read_per_mtok": 0.25,
        "cache_create_per_mtok": 0.0,
        "notes": "GPT-5.4 standard (backdated — see 2026-05-01 row's comment)",
    },
    {
        "provider_id": "chatgpt",
        "model_id": "gpt-5.4-mini",
        "effective_from": "2025-08-01",
        "input_per_mtok": 0.75,
        "output_per_mtok": 4.50,
        "cache_read_per_mtok": 0.075,
        "cache_create_per_mtok": 0.0,
        "notes": "GPT-5.4 Mini (backdated — see 2026-05-01 row's comment)",
    },
    # OpenAI ChatGPT — legacy ids never previously seeded (fell through to the
    # bare "gpt-5"/"codex" bucket before full-slug preservation). Backdated to
    # 2025-08-01 so historical events under these exact ids price instead of
    # falling to $0 — this applies current published rates retroactively,
    # which is the best available option with no historical price series.
    {
        "provider_id": "chatgpt",
        "model_id": "gpt-5.1",
        "effective_from": "2025-08-01",
        "input_per_mtok": 1.25,
        "output_per_mtok": 10.00,
        "cache_read_per_mtok": 0.125,
        "cache_create_per_mtok": 0.0,
        "notes": "GPT-5.1 standard",
    },
    {
        "provider_id": "chatgpt",
        "model_id": "gpt-5.2",
        "effective_from": "2025-08-01",
        "input_per_mtok": 1.75,
        "output_per_mtok": 14.00,
        "cache_read_per_mtok": 0.175,
        "cache_create_per_mtok": 0.0,
        "notes": "GPT-5.2 standard",
    },
    {
        "provider_id": "chatgpt",
        "model_id": "gpt-5-mini",
        "effective_from": "2025-08-01",
        "input_per_mtok": 0.25,
        "output_per_mtok": 2.00,
        "cache_read_per_mtok": 0.025,
        "cache_create_per_mtok": 0.0,
        "notes": "GPT-5 Mini",
    },
    {
        "provider_id": "chatgpt",
        "model_id": "gpt-5-nano",
        "effective_from": "2025-08-01",
        "input_per_mtok": 0.05,
        "output_per_mtok": 0.40,
        "cache_read_per_mtok": 0.005,
        "cache_create_per_mtok": 0.0,
        "notes": "GPT-5 Nano",
    },
    {
        "provider_id": "chatgpt",
        "model_id": "gpt-5.3-codex",
        "effective_from": "2025-08-01",
        "input_per_mtok": 1.75,
        "output_per_mtok": 14.00,
        "cache_read_per_mtok": 0.175,
        "cache_create_per_mtok": 0.0,
        "notes": "gpt-5.3-codex (Standard)",
    },
    # OpenAI ChatGPT — other codex slugs. Not separately published; the
    # pricing page lists only gpt-5.3-codex, so these inherit its rate rather
    # than falling through to $0 now that full slugs are preserved (previously
    # they all normalized to the bare "codex" bucket above, which is priced).
    {
        "provider_id": "chatgpt",
        "model_id": "gpt-5-codex",
        "effective_from": "2025-08-01",
        "input_per_mtok": 1.75,
        "output_per_mtok": 14.00,
        "cache_read_per_mtok": 0.175,
        "cache_create_per_mtok": 0.0,
        "notes": "rate inherited from gpt-5.3-codex — not separately published",
    },
    {
        "provider_id": "chatgpt",
        "model_id": "gpt-5.1-codex",
        "effective_from": "2025-08-01",
        "input_per_mtok": 1.75,
        "output_per_mtok": 14.00,
        "cache_read_per_mtok": 0.175,
        "cache_create_per_mtok": 0.0,
        "notes": "rate inherited from gpt-5.3-codex — not separately published",
    },
    {
        "provider_id": "chatgpt",
        "model_id": "gpt-5.1-codex-max",
        "effective_from": "2025-08-01",
        "input_per_mtok": 1.75,
        "output_per_mtok": 14.00,
        "cache_read_per_mtok": 0.175,
        "cache_create_per_mtok": 0.0,
        "notes": "rate inherited from gpt-5.3-codex — not separately published",
    },
    # Google Gemini — coarse buckets kept for legacy events ingested before the
    # extractor split into versioned ids. New events go to *-2.5 / *-3.1-preview.
    {
        "provider_id": "gemini",
        "model_id": "pro",
        "effective_from": "2025-09-01",
        "input_per_mtok": 1.25,
        "output_per_mtok": 10.00,
        "cache_read_per_mtok": 0.125,
        "cache_create_per_mtok": 0.0,
        "notes": "DEPRECATED — legacy bucket, superseded by pro-2.5 from 2026-05-17",
    },
    {
        "provider_id": "gemini",
        "model_id": "flash",
        "effective_from": "2025-09-01",
        "input_per_mtok": 0.30,
        "output_per_mtok": 2.50,
        "cache_read_per_mtok": 0.03,
        "cache_create_per_mtok": 0.0,
        "notes": "DEPRECATED — legacy bucket, superseded by flash-2.5 from 2026-05-17",
    },
    {
        "provider_id": "gemini",
        "model_id": "flash-lite",
        "effective_from": "2025-09-01",
        "input_per_mtok": 0.10,
        "output_per_mtok": 0.40,
        "cache_read_per_mtok": 0.01,
        "cache_create_per_mtok": 0.0,
        "notes": "DEPRECATED — legacy bucket, superseded by flash-lite-2.5 from 2026-05-17",
    },
    # Official rates per https://ai.google.dev/gemini-api/docs/pricing (paid tier,
    # text/image/video). Backdated to 2025-09-01 (when 2.5 first appeared in this
    # seed) so historical events relabeled by scripts/fix_gemini_model_ids.py
    # find a matching pricing row — the rates themselves haven't changed; the
    # original seed just had the wrong cache-read values.
    # Tiered >200K-token pricing is not modeled (schema would need a tier
    # column); 2.5 Pro long-context calls undercount slightly.
    {
        "provider_id": "gemini",
        "model_id": "pro-2.5",
        "effective_from": "2025-09-01",
        "input_per_mtok": 1.25,
        "output_per_mtok": 10.00,
        "cache_read_per_mtok": 0.125,
        "cache_create_per_mtok": 0.0,
        "notes": "Gemini 2.5 Pro",
    },
    {
        "provider_id": "gemini",
        "model_id": "flash-2.5",
        "effective_from": "2025-09-01",
        "input_per_mtok": 0.30,
        "output_per_mtok": 2.50,
        "cache_read_per_mtok": 0.03,
        "cache_create_per_mtok": 0.0,
        "notes": "Gemini 2.5 Flash",
    },
    {
        "provider_id": "gemini",
        "model_id": "flash-lite-2.5",
        "effective_from": "2025-09-01",
        "input_per_mtok": 0.10,
        "output_per_mtok": 0.40,
        "cache_read_per_mtok": 0.01,
        "cache_create_per_mtok": 0.0,
        "notes": "Gemini 2.5 Flash Lite",
    },
    # Gemini 3.x — standard tier, text/image/video, prompts ≤200K tokens
    # (https://ai.google.dev/gemini-api/docs/pricing). Tiered >200K pricing
    # isn't modeled by this schema; long-context calls undercount slightly.
    # Backdated to 2025-09-01 (matching the 2.5 family) so events relabeled
    # by scripts/fix_gemini_3x_relabel.py find a matching pricing row —
    # Google's rate hasn't changed since these models launched. The 2026-05-17
    # row is kept as the "current" anchor in case future rate changes are
    # added with a later effective_from.
    {
        "provider_id": "gemini",
        "model_id": "pro-3.1-preview",
        "effective_from": "2025-09-01",
        "input_per_mtok": 2.00,
        "output_per_mtok": 12.00,
        "cache_read_per_mtok": 0.20,
        "cache_create_per_mtok": 0.0,
        "notes": "Gemini 3.1 Pro Preview",
    },
    {
        "provider_id": "gemini",
        "model_id": "pro-3.1-preview",
        "effective_from": "2026-05-17",
        "input_per_mtok": 2.00,
        "output_per_mtok": 12.00,
        "cache_read_per_mtok": 0.20,
        "cache_create_per_mtok": 0.0,
        "notes": "Gemini 3.1 Pro Preview",
    },
    {
        "provider_id": "gemini",
        "model_id": "flash-3-preview",
        "effective_from": "2025-09-01",
        "input_per_mtok": 0.50,
        "output_per_mtok": 3.00,
        "cache_read_per_mtok": 0.05,
        "cache_create_per_mtok": 0.0,
        "notes": "Gemini 3 Flash Preview",
    },
    {
        "provider_id": "gemini",
        "model_id": "flash-3-preview",
        "effective_from": "2026-05-17",
        "input_per_mtok": 0.50,
        "output_per_mtok": 3.00,
        "cache_read_per_mtok": 0.05,
        "cache_create_per_mtok": 0.0,
        "notes": "Gemini 3 Flash Preview",
    },
    {
        "provider_id": "gemini",
        "model_id": "flash-lite-3.1",
        "effective_from": "2025-09-01",
        "input_per_mtok": 0.25,
        "output_per_mtok": 1.50,
        "cache_read_per_mtok": 0.025,
        "cache_create_per_mtok": 0.0,
        "notes": "Gemini 3.1 Flash-Lite",
    },
    {
        "provider_id": "gemini",
        "model_id": "flash-lite-3.1",
        "effective_from": "2026-05-17",
        "input_per_mtok": 0.25,
        "output_per_mtok": 1.50,
        "cache_read_per_mtok": 0.025,
        "cache_create_per_mtok": 0.0,
        "notes": "Gemini 3.1 Flash-Lite",
    },
    # OpenCode (cost is on each event already; pricing rows here are fallback only)
    # ── MiniMax ──────────────────────────────────────────────────────────────────
    # Pay-as-you-go API rates (USD/Mtok), used to give Coding-Plan subscription
    # traffic (cost_usd=None on ingest — see _OC_CANONICAL_MAP in
    # scripts/sidecar_pkg/event_extractors/opencode.py) a notional "what this
    # would have cost on the API" figure, the same way Anthropic subscription
    # usage gets priced against Claude API rates above.
    # M3 has an undocumented >512k-input pricing cliff ($0.60/$2.40, cache read
    # $0.12) that this schema doesn't model per-event — fine at the token
    # volumes seen so far (well under 512k), but a >512k call will undercount.
    {
        "provider_id": "minimax",
        "model_id": "MiniMax-M3",
        "effective_from": "2026-06-01",
        "input_per_mtok": 0.30,
        "output_per_mtok": 1.20,
        "cache_read_per_mtok": 0.06,
        "cache_create_per_mtok": 0.0,
        "notes": "MiniMax M3, standard tier ≤512k input. High confidence — "
        "OpenRouter and independent aggregators agree.",
    },
    {
        "provider_id": "minimax",
        "model_id": "MiniMax-M2.7",
        "effective_from": "2026-06-01",
        "input_per_mtok": 0.30,
        "output_per_mtok": 1.20,
        "cache_read_per_mtok": 0.06,
        "cache_create_per_mtok": 0.375,
        "notes": "MiniMax M2.7. Medium confidence — not observed in our own "
        "event data yet; verify against platform.minimax.io before relying on it.",
    },
    {
        "provider_id": "minimax",
        "model_id": "MiniMax-M2.5",
        "effective_from": "2026-06-01",
        "input_per_mtok": 0.30,
        "output_per_mtok": 1.20,
        "cache_read_per_mtok": 0.03,
        "cache_create_per_mtok": 0.375,
        "notes": "MiniMax M2.5 (legacy). Medium confidence — one source instead "
        "quotes $0.27/$0.95; verify against platform.minimax.io before relying on it.",
    },
    {
        "provider_id": "minimax",
        "model_id": "MiniMax-M2",
        "effective_from": "2026-06-01",
        "input_per_mtok": 0.255,
        "output_per_mtok": 1.02,
        "cache_read_per_mtok": 0.03,
        "cache_create_per_mtok": 0.375,
        "notes": "MiniMax M2 (legacy). Low confidence — single third-party source, "
        "not cross-checked; verify against platform.minimax.io before relying on it.",
    },
    # ── Kimi Coding ──────────────────────────────────────────────────────────────
    # Notional "what this would have cost on the Moonshot API" figures for Kimi
    # For Coding subscription traffic (cost_usd=None on ingest — see
    # _OC_CANONICAL_MAP in scripts/sidecar_pkg/event_extractors/opencode.py),
    # the same pattern as MiniMax above. Rates per 1M tokens from
    # https://platform.kimi.ai/docs/pricing/chat (verified 2026-09-18).
    # Cache-create pricing is not published -> 0.0.
    {
        "provider_id": "kimi_coding",
        "model_id": "kimi-for-coding",
        "effective_from": "2026-09-18",
        "input_per_mtok": 0.95,
        "output_per_mtok": 4.00,
        "cache_read_per_mtok": 0.19,
        "cache_create_per_mtok": 0.0,
        "notes": "Alias for the coding-plan default model (currently the 2.8 "
        "preview) — kimi-k2.7-code rates used until 2.8 pricing is published.",
    },
    {
        "provider_id": "kimi_coding",
        "model_id": "kimi-2.8-preview",
        "effective_from": "2026-09-18",
        "input_per_mtok": 0.95,
        "output_per_mtok": 4.00,
        "cache_read_per_mtok": 0.19,
        "cache_create_per_mtok": 0.0,
        "notes": "Kimi 2.8 preview — proxy kimi-k2.7-code rates until official "
        "2.8 pricing is published. Low confidence.",
    },
    {
        "provider_id": "kimi_coding",
        "model_id": "k2.7-code",
        "effective_from": "2026-09-18",
        "input_per_mtok": 0.95,
        "output_per_mtok": 4.00,
        "cache_read_per_mtok": 0.19,
        "cache_create_per_mtok": 0.0,
        "notes": "kimi-k2.7-code official rates (platform.kimi.ai).",
    },
    {
        "provider_id": "kimi_coding",
        "model_id": "k2.7-code-highspeed",
        "effective_from": "2026-09-18",
        "input_per_mtok": 1.90,
        "output_per_mtok": 8.00,
        "cache_read_per_mtok": 0.38,
        "cache_create_per_mtok": 0.0,
        "notes": "kimi-k2.7-code-highspeed official rates (platform.kimi.ai).",
    },
    {
        "provider_id": "kimi_coding",
        "model_id": "k2.6",
        "effective_from": "2026-09-18",
        "input_per_mtok": 0.95,
        "output_per_mtok": 4.00,
        "cache_read_per_mtok": 0.16,
        "cache_create_per_mtok": 0.0,
        "notes": "kimi-k2.6 official rates (legacy).",
    },
    {
        "provider_id": "kimi_coding",
        "model_id": "k3-256k",
        "effective_from": "2026-09-18",
        "input_per_mtok": 3.00,
        "output_per_mtok": 15.00,
        "cache_read_per_mtok": 0.30,
        "cache_create_per_mtok": 0.0,
        "notes": "Kimi K3 rates (platform.kimi.ai/docs/pricing/chat); observed as "
        "the kimi-code-plan-global backend modelID in OpenCode events.",
    },
    # ── Antigravity ──────────────────────────────────────────────────────────────
    # Gemini models (standard tier — mirrors existing gemini pro-3.1-preview /
    # flash-3-preview / flash-lite-3.1 values).  Priority-tier is not modeled.
    {
        "provider_id": "antigravity",
        "model_id": "pro-3",
        "effective_from": "2025-09-01",
        "input_per_mtok": 2.00,
        "output_per_mtok": 12.00,
        "cache_read_per_mtok": 0.20,
        "cache_create_per_mtok": 0.0,
        "notes": "Antigravity Gemini 3.x Pro (standard tier)",
    },
    {
        "provider_id": "antigravity",
        "model_id": "flash-3",
        "effective_from": "2025-09-01",
        "input_per_mtok": 0.50,
        "output_per_mtok": 3.00,
        "cache_read_per_mtok": 0.05,
        "cache_create_per_mtok": 0.0,
        "notes": "Antigravity Gemini 3.x Flash (standard tier)",
    },
    {
        "provider_id": "antigravity",
        "model_id": "flash-lite-3",
        "effective_from": "2025-09-01",
        "input_per_mtok": 0.25,
        "output_per_mtok": 1.50,
        "cache_read_per_mtok": 0.025,
        "cache_create_per_mtok": 0.0,
        "notes": "Antigravity Gemini 3.x Flash-Lite (standard tier)",
    },
    # Versioned Gemini family buckets — _normalize_ag_model now preserves the
    # minor version from the display name / raw id when it starts with 3.x.
    # Rates per https://ai.google.dev/gemini-api/docs/pricing (paid tier).
    {
        "provider_id": "antigravity",
        "model_id": "flash-3.5",
        "effective_from": "2025-09-01",
        "input_per_mtok": 1.50,
        "output_per_mtok": 9.00,
        "cache_read_per_mtok": 0.15,
        "cache_create_per_mtok": 0.0,
        "notes": "Antigravity Gemini 3.5 Flash (standard tier)",
    },
    {
        "provider_id": "antigravity",
        "model_id": "flash-3.6",
        "effective_from": "2025-09-01",
        "input_per_mtok": 1.50,
        "output_per_mtok": 7.50,
        "cache_read_per_mtok": 0.15,
        "cache_create_per_mtok": 0.0,
        "notes": "Antigravity Gemini 3.6 Flash (standard tier)",
    },
    {
        "provider_id": "antigravity",
        "model_id": "flash-3.7",
        "effective_from": "2025-09-01",
        "input_per_mtok": 0.75,
        "output_per_mtok": 3.75,
        "cache_read_per_mtok": 0.075,
        "cache_create_per_mtok": 0.0,
        "notes": "Antigravity Gemini 3.7 Flash (standard tier)",
    },
    {
        "provider_id": "antigravity",
        "model_id": "flash-3.8",
        "effective_from": "2025-09-01",
        "input_per_mtok": 0.75,
        "output_per_mtok": 3.75,
        "cache_read_per_mtok": 0.075,
        "cache_create_per_mtok": 0.0,
        "notes": "Antigravity Gemini 3.8 Flash (standard tier)",
    },
    {
        "provider_id": "antigravity",
        "model_id": "pro-3.1",
        "effective_from": "2025-09-01",
        "input_per_mtok": 2.00,
        "output_per_mtok": 12.00,
        "cache_read_per_mtok": 0.20,
        "cache_create_per_mtok": 0.0,
        "notes": "Antigravity Gemini 3.1 Pro (standard tier)",
    },
    {
        "provider_id": "antigravity",
        "model_id": "pro",
        "effective_from": "2025-09-01",
        "input_per_mtok": 1.25,
        "output_per_mtok": 10.00,
        "cache_read_per_mtok": 0.125,
        "cache_create_per_mtok": 0.0,
        "notes": "Antigravity Gemini Pro, no version in raw/display "
        "(https://ai.google.dev/gemini-api/docs/pricing)",
    },
    {
        "provider_id": "antigravity",
        "model_id": "flash",
        "effective_from": "2025-09-01",
        "input_per_mtok": 0.30,
        "output_per_mtok": 2.50,
        "cache_read_per_mtok": 0.03,
        "cache_create_per_mtok": 0.0,
        "notes": "Antigravity Gemini Flash, no version in raw/display "
        "(https://ai.google.dev/gemini-api/docs/pricing)",
    },
    {
        "provider_id": "antigravity",
        "model_id": "flash-lite",
        "effective_from": "2025-09-01",
        "input_per_mtok": 0.10,
        "output_per_mtok": 0.40,
        "cache_read_per_mtok": 0.01,
        "cache_create_per_mtok": 0.0,
        "notes": "Antigravity Gemini Flash-Lite, no 3.x signal "
        "(https://ai.google.dev/gemini-api/docs/pricing)",
    },
    {
        "provider_id": "antigravity",
        "model_id": "gemini-default",
        "effective_from": "2025-09-01",
        "input_per_mtok": 1.50,
        "output_per_mtok": 9.00,
        "cache_read_per_mtok": 0.15,
        "cache_create_per_mtok": 0.0,
        "notes": "Antigravity raw gemini-default placeholder with no family in "
        "display name; billed at Gemini 3.5 Flash rates "
        "(https://ai.google.dev/gemini-api/docs/pricing)",
    },
    # Claude models routed through Antigravity (official Claude API pricing).
    {
        "provider_id": "antigravity",
        "model_id": "claude-opus",
        "effective_from": "2025-09-01",
        "input_per_mtok": 5.00,
        "output_per_mtok": 25.00,
        "cache_read_per_mtok": 0.50,
        "cache_create_per_mtok": 6.25,
        "notes": "Antigravity Claude Opus 4.x",
    },
    {
        "provider_id": "antigravity",
        "model_id": "claude-sonnet",
        "effective_from": "2025-09-01",
        "input_per_mtok": 3.00,
        "output_per_mtok": 15.00,
        "cache_read_per_mtok": 0.30,
        "cache_create_per_mtok": 3.75,
        "notes": "Antigravity Claude Sonnet 4.x",
    },
    # ── xAI (Grok) ─────────────────────────────────────────────────────────────
    # Rates per https://docs.x.ai/developers/pricing (identical table at
    # /developers/models), checked 2026-09-26. USD per 1M tokens.
    # - cache_create_per_mtok = 0.0: xAI publishes no cache-write fee; cached
    #   prompt tokens bill at the cached-input rate.
    # - effective_from is backdated to 2025-07-01 (grok-4 launch era) so every
    #   historical xai event finds a row — cost_calculator only applies rows
    #   with effective_from <= ts.date(), and no historical price series exists
    #   (same backdating rationale as the chatgpt/gemini rows above).
    # - The >=200k-prompt long-context tier (2x for all tokens), Batch API -20%,
    #   Priority Processing 2x, and the US regional endpoint 1.1x are NOT
    #   modeled — provider_pricing has no tier/multiplier column (same
    #   limitation as Gemini's >200K pricing). Long prompts undercount.
    # - Seed is a fallback only: EventIngestor prefers sidecar-reported
    #   cost_usd (costUsdTicks) over the computed breakdown. Do not run
    #   scripts/recost_events.py --provider xai — Phase B would overwrite
    #   CLI-reported costs.
    # Documented ids (current pricing page):
    {
        "provider_id": "xai",
        "model_id": "grok-4.7",
        "effective_from": "2025-07-01",
        "input_per_mtok": 2.00,
        "output_per_mtok": 6.00,
        "cache_read_per_mtok": 0.50,
        "cache_create_per_mtok": 0.0,
        "notes": "Grok 4.7, <200k prompt tier (>=200k bills 2x, not modeled)",
    },
    {
        "provider_id": "xai",
        "model_id": "grok-4.6",
        "effective_from": "2025-07-01",
        "input_per_mtok": 2.00,
        "output_per_mtok": 6.00,
        "cache_read_per_mtok": 0.50,
        "cache_create_per_mtok": 0.0,
        "notes": "Grok 4.6, <200k prompt tier (>=200k bills 2x, not modeled)",
    },
    {
        "provider_id": "xai",
        "model_id": "grok-4.5",
        "effective_from": "2025-07-01",
        "input_per_mtok": 2.00,
        "output_per_mtok": 6.00,
        "cache_read_per_mtok": 0.30,
        "cache_create_per_mtok": 0.0,
        "notes": "Grok 4.5, <200k prompt tier (>=200k bills 2x, not modeled)",
    },
    {
        "provider_id": "xai",
        "model_id": "grok-4.3",
        "effective_from": "2025-07-01",
        "input_per_mtok": 1.25,
        "output_per_mtok": 2.50,
        "cache_read_per_mtok": 0.20,
        "cache_create_per_mtok": 0.0,
        "notes": "Grok 4.3, <200k prompt tier (>=200k bills 2x, not modeled)",
    },
    {
        "provider_id": "xai",
        "model_id": "grok-4.20-0309-reasoning",
        "effective_from": "2025-07-01",
        "input_per_mtok": 1.25,
        "output_per_mtok": 2.50,
        "cache_read_per_mtok": 0.20,
        "cache_create_per_mtok": 0.0,
        "notes": "Grok 4.20 0309 reasoning, <200k prompt tier (not modeled)",
    },
    {
        "provider_id": "xai",
        "model_id": "grok-4.20-0309-non-reasoning",
        "effective_from": "2025-07-01",
        "input_per_mtok": 1.25,
        "output_per_mtok": 2.50,
        "cache_read_per_mtok": 0.20,
        "cache_create_per_mtok": 0.0,
        "notes": "Grok 4.20 0309 non-reasoning, <200k prompt tier (not modeled)",
    },
    {
        "provider_id": "xai",
        "model_id": "grok-4.20-multi-agent-0309",
        "effective_from": "2025-07-01",
        "input_per_mtok": 1.25,
        "output_per_mtok": 2.50,
        "cache_read_per_mtok": 0.20,
        "cache_create_per_mtok": 0.0,
        "notes": "Grok 4.20 multi-agent 0309, <200k prompt tier (not modeled)",
    },
    {
        "provider_id": "xai",
        "model_id": "grok-build-0.1",
        "effective_from": "2025-07-01",
        "input_per_mtok": 1.00,
        "output_per_mtok": 2.00,
        "cache_read_per_mtok": 0.20,
        "cache_create_per_mtok": 0.0,
        "notes": "Grok Build 0.1, <200k prompt tier (256k ctx, not modeled)",
    },
    # Observed ids not on the current pricing page.
    {
        "provider_id": "xai",
        "model_id": "grok-build",
        "effective_from": "2025-07-01",
        "input_per_mtok": 1.00,
        "output_per_mtok": 2.00,
        "cache_read_per_mtok": 0.20,
        "cache_create_per_mtok": 0.0,
        "notes": "Grok CLI signals.json primaryModelId; rates inherited from "
        "grok-build-0.1 — not separately published",
    },
    {
        "provider_id": "xai",
        "model_id": "grok-4",
        "effective_from": "2025-07-01",
        "input_per_mtok": 3.00,
        "output_per_mtok": 15.00,
        "cache_read_per_mtok": 0.75,
        "cache_create_per_mtok": 0.0,
        "notes": "Grok 4 (docs.x.ai/docs/models/grok-4-0709, delisted from the "
        "current pricing page)",
    },
    {
        "provider_id": "xai",
        "model_id": "grok-4-fast",
        "effective_from": "2025-07-01",
        "input_per_mtok": 0.20,
        "output_per_mtok": 0.50,
        "cache_read_per_mtok": 0.05,
        "cache_create_per_mtok": 0.0,
        "notes": "Grok 4 Fast (official model page, delisted from the current pricing page)",
    },
    {
        "provider_id": "xai",
        "model_id": "grok-code-fast-1",
        "effective_from": "2025-07-01",
        "input_per_mtok": 0.20,
        "output_per_mtok": 1.50,
        "cache_read_per_mtok": 0.02,
        "cache_create_per_mtok": 0.0,
        "notes": "grok-code-fast-1 — medium confidence, multi-source "
        "(metronome.com/pricing-index/xai-api, aicomp.prygn.com); not on the "
        "current pricing page",
    },
    # Deliberately unseeded (stay $0): grok-4.1, grok-4-mini — seen only in
    # test fixtures, no official current rate found; a proxy rate was rejected
    # in favour of leaving them unpriced (issue #346).
    # GPT-OSS 120B: no row — cost defaults to 0.
]


def seed_pricing_table(session: Session) -> int:
    """Insert any seed rows missing from provider_pricing. Returns rows inserted."""
    inserted = 0
    for row in PRICING_SEED:
        exists = session.exec(
            select(ProviderPricing).where(
                ProviderPricing.provider_id == row["provider_id"],
                ProviderPricing.model_id == row["model_id"],
                ProviderPricing.effective_from == date.fromisoformat(row["effective_from"]),
            )
        ).first()
        if exists:
            # cache_create_1h_per_mtok postdates the original seed rows (added
            # when Runway started splitting cache writes by TTL). This isn't a
            # real-world price change, so backfill it onto the already-seeded
            # row in place rather than versioning a new effective_from row —
            # but only when it's still at the column's default, so a rate a
            # user tuned by hand is never clobbered.
            seed_1h = row.get("cache_create_1h_per_mtok", 0.0)
            if exists.cache_create_1h_per_mtok == 0.0 and seed_1h:
                exists.cache_create_1h_per_mtok = seed_1h
                session.add(exists)
            continue
        session.add(
            ProviderPricing(
                provider_id=row["provider_id"],
                model_id=row["model_id"],
                effective_from=date.fromisoformat(row["effective_from"]),
                input_per_mtok=row["input_per_mtok"],
                output_per_mtok=row["output_per_mtok"],
                cache_read_per_mtok=row["cache_read_per_mtok"],
                cache_create_per_mtok=row["cache_create_per_mtok"],
                cache_create_1h_per_mtok=row.get("cache_create_1h_per_mtok", 0.0),
                notes=row.get("notes"),
            )
        )
        inserted += 1
    session.commit()
    return inserted
