"""Seed provider_pricing with current public rates.

Add new rows (don't modify existing) when prices change — the
effective_from column is the natural version key.
"""

from datetime import date

from sqlmodel import Session, select

from app.models.db import ProviderPricing

PRICING_SEED: list[dict] = [
    # Anthropic Claude (Sonnet 4.5, Opus 4.5, Haiku 4.5)
    {
        "provider_id": "anthropic",
        "model_id": "sonnet",
        "effective_from": "2025-09-01",
        "input_per_mtok": 3.00,
        "output_per_mtok": 15.00,
        "cache_read_per_mtok": 0.30,
        "cache_create_per_mtok": 3.75,
        "notes": "Sonnet 4.5",
    },
    {
        "provider_id": "anthropic",
        "model_id": "opus",
        "effective_from": "2025-09-01",
        "input_per_mtok": 15.00,
        "output_per_mtok": 75.00,
        "cache_read_per_mtok": 1.50,
        "cache_create_per_mtok": 18.75,
        "notes": "Opus 4.5",
    },
    {
        "provider_id": "anthropic",
        "model_id": "haiku",
        "effective_from": "2025-09-01",
        "input_per_mtok": 0.80,
        "output_per_mtok": 4.00,
        "cache_read_per_mtok": 0.08,
        "cache_create_per_mtok": 1.00,
        "notes": "Haiku 4.5",
    },
    # OpenAI ChatGPT / Codex (GPT-5 series)
    {
        "provider_id": "chatgpt",
        "model_id": "gpt-5",
        "effective_from": "2025-08-01",
        "input_per_mtok": 5.00,
        "output_per_mtok": 15.00,
        "cache_read_per_mtok": 1.25,
        "cache_create_per_mtok": 0.0,
        "notes": "GPT-5 standard",
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
                notes=row.get("notes"),
            )
        )
        inserted += 1
    session.commit()
    return inserted
