from datetime import date

from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.models.db import ProviderPricing
from app.services.pricing_seed import PRICING_SEED, seed_pricing_table


def _make_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_seed_inserts_all_rows_on_empty_db():
    s = _make_session()
    seed_pricing_table(s)
    rows = s.exec(select(ProviderPricing)).all()
    assert len(rows) == len(PRICING_SEED)


def test_seed_is_idempotent():
    s = _make_session()
    seed_pricing_table(s)
    seed_pricing_table(s)  # second call should be a no-op
    rows = s.exec(select(ProviderPricing)).all()
    assert len(rows) == len(PRICING_SEED)


def test_seed_chatgpt_gpt54_mini_rates():
    s = _make_session()
    seed_pricing_table(s)
    row = s.exec(
        select(ProviderPricing).where(
            ProviderPricing.provider_id == "chatgpt",
            ProviderPricing.model_id == "gpt-5.4-mini",
        )
    ).first()
    assert row is not None
    assert row.input_per_mtok == 0.75
    assert row.output_per_mtok == 4.50
    assert row.cache_read_per_mtok == 0.075
    assert row.cache_create_per_mtok == 0.0


def test_seed_chatgpt_gpt6_sol_rates():
    """Per developers.openai.com/api/docs/pricing (checked 2026-09-27).

    Cache-write is published for this generation at 1.25x input (issue #369).
    """
    s = _make_session()
    seed_pricing_table(s)
    row = s.exec(
        select(ProviderPricing).where(
            ProviderPricing.provider_id == "chatgpt",
            ProviderPricing.model_id == "gpt-6-sol",
        )
    ).first()
    assert row is not None
    assert row.input_per_mtok == 2.00
    assert row.output_per_mtok == 10.00
    assert row.cache_read_per_mtok == 0.20
    assert row.cache_create_per_mtok == 2.50


def test_seed_chatgpt_gpt6_luna_rates():
    """Per developers.openai.com/api/docs/pricing (checked 2026-09-27).

    Cache-write is published for this generation at 1.25x input (issue #369).
    """
    s = _make_session()
    seed_pricing_table(s)
    row = s.exec(
        select(ProviderPricing).where(
            ProviderPricing.provider_id == "chatgpt",
            ProviderPricing.model_id == "gpt-6-luna",
        )
    ).first()
    assert row is not None
    assert row.input_per_mtok == 0.10
    assert row.output_per_mtok == 0.50
    assert row.cache_read_per_mtok == 0.01
    assert row.cache_create_per_mtok == 0.125


def test_seed_chatgpt_gpt6_astra_rates():
    """Per developers.openai.com/api/docs/pricing (checked 2026-09-27).

    Cache-write is published for this generation at 1.25x input (issue #369).
    """
    s = _make_session()
    seed_pricing_table(s)
    row = s.exec(
        select(ProviderPricing).where(
            ProviderPricing.provider_id == "chatgpt",
            ProviderPricing.model_id == "gpt-6-astra",
        )
    ).first()
    assert row is not None
    assert row.input_per_mtok == 10.00
    assert row.output_per_mtok == 50.00
    assert row.cache_read_per_mtok == 1.00
    assert row.cache_create_per_mtok == 12.50


def test_seed_chatgpt_gpt56_terra_and_luna_cache_write_rates():
    """gpt-5.6-terra / gpt-5.6-luna cache-write at 1.25x input (issue #369)."""
    s = _make_session()
    seed_pricing_table(s)
    terra = s.exec(
        select(ProviderPricing).where(
            ProviderPricing.provider_id == "chatgpt",
            ProviderPricing.model_id == "gpt-5.6-terra",
        )
    ).first()
    assert terra is not None
    assert terra.cache_create_per_mtok == 2.50

    luna = s.exec(
        select(ProviderPricing).where(
            ProviderPricing.provider_id == "chatgpt",
            ProviderPricing.model_id == "gpt-5.6-luna",
        )
    ).first()
    assert luna is not None
    assert luna.cache_create_per_mtok == 0.25


def test_seed_chatgpt_bare_gpt56_inherits_sol_rate():
    """Bare gpt-5.6 (pre-codename-preservation events) prices at the
    generation's standard (sol) tier rather than falling to $0."""
    s = _make_session()
    seed_pricing_table(s)
    row = s.exec(
        select(ProviderPricing).where(
            ProviderPricing.provider_id == "chatgpt",
            ProviderPricing.model_id == "gpt-5.6",
        )
    ).first()
    assert row is not None
    assert row.input_per_mtok == 4.00
    assert row.output_per_mtok == 20.00
    assert row.cache_read_per_mtok == 0.40
    assert row.cache_create_per_mtok == 5.00  # 1.25x input, inherited from gpt-5.6-sol (#369)


def test_seed_chatgpt_gpt56_sol_cache_write_rate():
    """gpt-5.6-sol cache-write at 1.25x input (issue #369)."""
    s = _make_session()
    seed_pricing_table(s)
    row = s.exec(
        select(ProviderPricing).where(
            ProviderPricing.provider_id == "chatgpt",
            ProviderPricing.model_id == "gpt-5.6-sol",
        )
    ).first()
    assert row is not None
    assert row.cache_create_per_mtok == 5.00


def test_seed_chatgpt_older_rows_stay_at_zero_cache_create():
    """Older chatgpt generations (pre gpt-5.6/gpt-6) have no published
    cache-write rate — cache_create_per_mtok must stay 0.0 (issue #369)."""
    s = _make_session()
    seed_pricing_table(s)
    for model_id in ("gpt-5", "codex", "gpt-5.5", "gpt-5.4", "gpt-5.3-codex", "gpt-5-mini"):
        row = s.exec(
            select(ProviderPricing).where(
                ProviderPricing.provider_id == "chatgpt",
                ProviderPricing.model_id == model_id,
            )
        ).first()
        assert row is not None, f"expected a seeded row for {model_id}"
        assert row.cache_create_per_mtok == 0.0, f"{model_id} should stay unpriced for cache writes"


def test_seed_backfills_cache_create_per_mtok_on_existing_row():
    """A row already committed to the DB with cache_create_per_mtok=0.0
    (simulating a pre-#369 deployment) gets backfilled in place on re-seed,
    the same way cache_create_1h_per_mtok is backfilled."""
    s = _make_session()
    s.add(
        ProviderPricing(
            provider_id="chatgpt",
            model_id="gpt-6-sol",
            effective_from=date.fromisoformat("2026-09-01"),
            input_per_mtok=2.00,
            output_per_mtok=10.00,
            cache_read_per_mtok=0.20,
            cache_create_per_mtok=0.0,
            cache_create_1h_per_mtok=0.0,
            notes="pre-#369 seed",
        )
    )
    s.commit()

    seed_pricing_table(s)

    row = s.exec(
        select(ProviderPricing).where(
            ProviderPricing.provider_id == "chatgpt",
            ProviderPricing.model_id == "gpt-6-sol",
        )
    ).first()
    assert row is not None
    assert row.cache_create_per_mtok == 2.50
    # No second row was versioned in — the backfill updates in place.
    rows = s.exec(
        select(ProviderPricing).where(
            ProviderPricing.provider_id == "chatgpt",
            ProviderPricing.model_id == "gpt-6-sol",
        )
    ).all()
    assert len(rows) == 1


def test_seed_does_not_clobber_user_hand_tuned_cache_create_rate():
    """A row a user already hand-set to a non-zero cache_create_per_mtok must
    not be overwritten by the seed's rate on re-seed."""
    s = _make_session()
    s.add(
        ProviderPricing(
            provider_id="chatgpt",
            model_id="gpt-6-sol",
            effective_from=date.fromisoformat("2026-09-01"),
            input_per_mtok=2.00,
            output_per_mtok=10.00,
            cache_read_per_mtok=0.20,
            cache_create_per_mtok=9.99,  # user hand-tuned this
            cache_create_1h_per_mtok=0.0,
            notes="user-tuned",
        )
    )
    s.commit()

    seed_pricing_table(s)

    row = s.exec(
        select(ProviderPricing).where(
            ProviderPricing.provider_id == "chatgpt",
            ProviderPricing.model_id == "gpt-6-sol",
        )
    ).first()
    assert row is not None
    assert row.cache_create_per_mtok == 9.99


def test_seed_preserves_anthropic_sonnet_rates():
    s = _make_session()
    seed_pricing_table(s)
    sonnet = s.exec(
        select(ProviderPricing).where(
            ProviderPricing.provider_id == "anthropic",
            ProviderPricing.model_id == "sonnet",
        )
    ).first()
    assert sonnet is not None
    assert sonnet.input_per_mtok == 3.00
    assert sonnet.output_per_mtok == 15.00
    assert sonnet.cache_read_per_mtok == 0.30
    assert sonnet.cache_create_per_mtok == 3.75


def test_seed_anthropic_fable_rates():
    """Per https://platform.claude.com/docs/en/about-claude/pricing (Fable 5)."""
    s = _make_session()
    seed_pricing_table(s)
    fable = s.exec(
        select(ProviderPricing).where(
            ProviderPricing.provider_id == "anthropic",
            ProviderPricing.model_id == "fable",
        )
    ).first()
    assert fable is not None
    assert fable.input_per_mtok == 10.00
    assert fable.output_per_mtok == 50.00
    assert fable.cache_read_per_mtok == 1.00
    assert fable.cache_create_per_mtok == 12.50


def _anthropic_row(model_id: str) -> ProviderPricing:
    s = _make_session()
    seed_pricing_table(s)
    row = s.exec(
        select(ProviderPricing).where(
            ProviderPricing.provider_id == "anthropic",
            ProviderPricing.model_id == model_id,
        )
    ).first()
    assert row is not None
    return row


def test_seed_anthropic_fable_5_1_cache_read_is_discounted():
    """Fable 5.1 cache hits are 0.025x input ($0.25), unlike Fable 5's $1.00."""
    row = _anthropic_row("fable-5.1")
    assert row.input_per_mtok == 10.00
    assert row.output_per_mtok == 50.00
    assert row.cache_read_per_mtok == 0.25
    assert row.cache_create_per_mtok == 12.50
    assert row.cache_create_1h_per_mtok == 20.00


def test_seed_anthropic_opus_5_5_rates():
    """Opus 5.5 is $4/$20, not the bare `opus` family's $5/$25."""
    row = _anthropic_row("opus-5.5")
    assert row.input_per_mtok == 4.00
    assert row.output_per_mtok == 20.00
    assert row.cache_read_per_mtok == 0.20
    assert row.cache_create_per_mtok == 5.00
    assert row.cache_create_1h_per_mtok == 8.00


def test_seed_anthropic_sonnet_5_and_5_5_rates():
    """Sonnet 5/5.5 are $2/$10 — not the bare `sonnet` (4.5) family's $3/$15."""
    for model_id in ("sonnet-5", "sonnet-5.5"):
        row = _anthropic_row(model_id)
        assert row.input_per_mtok == 2.00
        assert row.output_per_mtok == 10.00
        assert row.cache_read_per_mtok == 0.20
        assert row.cache_create_per_mtok == 2.50
        assert row.cache_create_1h_per_mtok == 4.00


def test_seed_chatgpt_gpt6_1_sol_rates():
    """GPT-6.1 Sol matches gpt-6-sol except cached input ($0.10 vs $0.20)."""
    s = _make_session()
    seed_pricing_table(s)
    row = s.exec(
        select(ProviderPricing).where(
            ProviderPricing.provider_id == "chatgpt",
            ProviderPricing.model_id == "gpt-6.1-sol",
        )
    ).first()
    assert row is not None
    assert row.input_per_mtok == 2.00
    assert row.output_per_mtok == 10.00
    assert row.cache_read_per_mtok == 0.10
    assert row.cache_create_per_mtok == 2.50


def test_seed_gemini_2_5_pro_rates_match_official():
    """Per https://ai.google.dev/gemini-api/docs/pricing (paid tier, ≤200K)."""
    s = _make_session()
    seed_pricing_table(s)
    pro = s.exec(
        select(ProviderPricing).where(
            ProviderPricing.provider_id == "gemini",
            ProviderPricing.model_id == "pro-2.5",
        )
    ).first()
    assert pro is not None
    assert pro.input_per_mtok == 1.25
    assert pro.output_per_mtok == 10.00
    assert pro.cache_read_per_mtok == 0.125
    assert pro.cache_create_per_mtok == 0.0


def test_seed_gemini_2_5_flash_rates_match_official():
    s = _make_session()
    seed_pricing_table(s)
    flash = s.exec(
        select(ProviderPricing).where(
            ProviderPricing.provider_id == "gemini",
            ProviderPricing.model_id == "flash-2.5",
        )
    ).first()
    assert flash is not None
    assert flash.input_per_mtok == 0.30
    assert flash.output_per_mtok == 2.50
    assert flash.cache_read_per_mtok == 0.03
    assert flash.cache_create_per_mtok == 0.0


def test_seed_gemini_2_5_flash_lite_rates_match_official():
    s = _make_session()
    seed_pricing_table(s)
    flash_lite = s.exec(
        select(ProviderPricing).where(
            ProviderPricing.provider_id == "gemini",
            ProviderPricing.model_id == "flash-lite-2.5",
        )
    ).first()
    assert flash_lite is not None
    assert flash_lite.input_per_mtok == 0.10
    assert flash_lite.output_per_mtok == 0.40
    assert flash_lite.cache_read_per_mtok == 0.01
    assert flash_lite.cache_create_per_mtok == 0.0


def test_seed_gemini_3_1_pro_preview_rates_match_official():
    """Per https://ai.google.dev/gemini-api/docs/pricing (paid tier, ≤200K)."""
    s = _make_session()
    seed_pricing_table(s)
    pro = s.exec(
        select(ProviderPricing).where(
            ProviderPricing.provider_id == "gemini",
            ProviderPricing.model_id == "pro-3.1-preview",
        )
    ).first()
    assert pro is not None
    assert pro.input_per_mtok == 2.00
    assert pro.output_per_mtok == 12.00
    assert pro.cache_read_per_mtok == 0.20
    assert pro.cache_create_per_mtok == 0.0


# ── Antigravity pricing rows ──────────────────────────────────────────────────


def test_seed_antigravity_pro3_rates():
    """Antigravity Gemini 3.x Pro mirrors the standard-tier gemini pro-3.1-preview rate."""
    s = _make_session()
    seed_pricing_table(s)
    row = s.exec(
        select(ProviderPricing).where(
            ProviderPricing.provider_id == "antigravity",
            ProviderPricing.model_id == "pro-3",
        )
    ).first()
    assert row is not None
    assert row.input_per_mtok == 2.00
    assert row.output_per_mtok == 12.00
    assert row.cache_read_per_mtok == 0.20
    assert row.cache_create_per_mtok == 0.0


def test_seed_antigravity_flash3_rates():
    """Antigravity Gemini 3.x Flash mirrors the standard-tier gemini flash-3-preview rate."""
    s = _make_session()
    seed_pricing_table(s)
    row = s.exec(
        select(ProviderPricing).where(
            ProviderPricing.provider_id == "antigravity",
            ProviderPricing.model_id == "flash-3",
        )
    ).first()
    assert row is not None
    assert row.input_per_mtok == 0.50
    assert row.output_per_mtok == 3.00
    assert row.cache_read_per_mtok == 0.05
    assert row.cache_create_per_mtok == 0.0


def test_seed_antigravity_flash_lite3_rates():
    s = _make_session()
    seed_pricing_table(s)
    row = s.exec(
        select(ProviderPricing).where(
            ProviderPricing.provider_id == "antigravity",
            ProviderPricing.model_id == "flash-lite-3",
        )
    ).first()
    assert row is not None
    assert row.input_per_mtok == 0.25
    assert row.output_per_mtok == 1.50
    assert row.cache_read_per_mtok == 0.025
    assert row.cache_create_per_mtok == 0.0


def test_seed_antigravity_flash_35_rates():
    """Per https://ai.google.dev/gemini-api/docs/pricing (paid tier)."""
    s = _make_session()
    seed_pricing_table(s)
    row = s.exec(
        select(ProviderPricing).where(
            ProviderPricing.provider_id == "antigravity",
            ProviderPricing.model_id == "flash-3.5",
        )
    ).first()
    assert row is not None
    assert row.input_per_mtok == 1.50
    assert row.output_per_mtok == 9.00
    assert row.cache_read_per_mtok == 0.15
    assert row.cache_create_per_mtok == 0.0


def test_seed_antigravity_flash_36_rates():
    s = _make_session()
    seed_pricing_table(s)
    row = s.exec(
        select(ProviderPricing).where(
            ProviderPricing.provider_id == "antigravity",
            ProviderPricing.model_id == "flash-3.6",
        )
    ).first()
    assert row is not None
    assert row.input_per_mtok == 1.50
    assert row.output_per_mtok == 7.50
    assert row.cache_read_per_mtok == 0.15
    assert row.cache_create_per_mtok == 0.0


def test_seed_antigravity_flash_37_rates():
    s = _make_session()
    seed_pricing_table(s)
    row = s.exec(
        select(ProviderPricing).where(
            ProviderPricing.provider_id == "antigravity",
            ProviderPricing.model_id == "flash-3.7",
        )
    ).first()
    assert row is not None
    assert row.input_per_mtok == 0.75
    assert row.output_per_mtok == 3.75
    assert row.cache_read_per_mtok == 0.075
    assert row.cache_create_per_mtok == 0.0


def test_seed_antigravity_flash_38_rates():
    s = _make_session()
    seed_pricing_table(s)
    row = s.exec(
        select(ProviderPricing).where(
            ProviderPricing.provider_id == "antigravity",
            ProviderPricing.model_id == "flash-3.8",
        )
    ).first()
    assert row is not None
    assert row.input_per_mtok == 0.75
    assert row.output_per_mtok == 3.75
    assert row.cache_read_per_mtok == 0.075
    assert row.cache_create_per_mtok == 0.0


def test_seed_antigravity_pro_31_rates():
    s = _make_session()
    seed_pricing_table(s)
    row = s.exec(
        select(ProviderPricing).where(
            ProviderPricing.provider_id == "antigravity",
            ProviderPricing.model_id == "pro-3.1",
        )
    ).first()
    assert row is not None
    assert row.input_per_mtok == 2.00
    assert row.output_per_mtok == 12.00
    assert row.cache_read_per_mtok == 0.20
    assert row.cache_create_per_mtok == 0.0


def test_seed_antigravity_bare_pro_rates():
    """Bare pro (no version) uses Gemini's lower 2.5-pro-equivalent rate."""
    s = _make_session()
    seed_pricing_table(s)
    row = s.exec(
        select(ProviderPricing).where(
            ProviderPricing.provider_id == "antigravity",
            ProviderPricing.model_id == "pro",
        )
    ).first()
    assert row is not None
    assert row.input_per_mtok == 1.25
    assert row.output_per_mtok == 10.00
    assert row.cache_read_per_mtok == 0.125
    assert row.cache_create_per_mtok == 0.0


def test_seed_antigravity_bare_flash_rates():
    s = _make_session()
    seed_pricing_table(s)
    row = s.exec(
        select(ProviderPricing).where(
            ProviderPricing.provider_id == "antigravity",
            ProviderPricing.model_id == "flash",
        )
    ).first()
    assert row is not None
    assert row.input_per_mtok == 0.30
    assert row.output_per_mtok == 2.50
    assert row.cache_read_per_mtok == 0.03
    assert row.cache_create_per_mtok == 0.0


def test_seed_antigravity_bare_flash_lite_rates():
    s = _make_session()
    seed_pricing_table(s)
    row = s.exec(
        select(ProviderPricing).where(
            ProviderPricing.provider_id == "antigravity",
            ProviderPricing.model_id == "flash-lite",
        )
    ).first()
    assert row is not None
    assert row.input_per_mtok == 0.10
    assert row.output_per_mtok == 0.40
    assert row.cache_read_per_mtok == 0.01
    assert row.cache_create_per_mtok == 0.0


def test_seed_antigravity_gemini_default_rates():
    """gemini-default with no family in display bills at Gemini 3.5 Flash rates."""
    s = _make_session()
    seed_pricing_table(s)
    row = s.exec(
        select(ProviderPricing).where(
            ProviderPricing.provider_id == "antigravity",
            ProviderPricing.model_id == "gemini-default",
        )
    ).first()
    assert row is not None
    assert row.input_per_mtok == 1.50
    assert row.output_per_mtok == 9.00
    assert row.cache_read_per_mtok == 0.15
    assert row.cache_create_per_mtok == 0.0


def test_seed_antigravity_claude_opus_rates():
    """Antigravity Claude Opus uses official Claude Opus 4.x pricing."""
    s = _make_session()
    seed_pricing_table(s)
    row = s.exec(
        select(ProviderPricing).where(
            ProviderPricing.provider_id == "antigravity",
            ProviderPricing.model_id == "claude-opus",
        )
    ).first()
    assert row is not None
    assert row.input_per_mtok == 5.00
    assert row.output_per_mtok == 25.00
    assert row.cache_read_per_mtok == 0.50
    assert row.cache_create_per_mtok == 6.25


def test_seed_antigravity_claude_sonnet_rates():
    """Antigravity Claude Sonnet uses official Claude Sonnet 4.x pricing."""
    s = _make_session()
    seed_pricing_table(s)
    row = s.exec(
        select(ProviderPricing).where(
            ProviderPricing.provider_id == "antigravity",
            ProviderPricing.model_id == "claude-sonnet",
        )
    ).first()
    assert row is not None
    assert row.input_per_mtok == 3.00
    assert row.output_per_mtok == 15.00
    assert row.cache_read_per_mtok == 0.30
    assert row.cache_create_per_mtok == 3.75


def test_seed_antigravity_no_gpt_oss_row():
    """GPT-OSS 120B is intentionally unpriced — no row means cost defaults to 0."""
    s = _make_session()
    seed_pricing_table(s)
    row = s.exec(
        select(ProviderPricing).where(
            ProviderPricing.provider_id == "antigravity",
            ProviderPricing.model_id == "gpt-oss",
        )
    ).first()
    assert row is None


# ── xAI (Grok) pricing rows ──────────────────────────────────────────────────


def test_seed_xai_grok43_rates():
    """Per https://docs.x.ai/developers/pricing (<200k prompt tier)."""
    s = _make_session()
    seed_pricing_table(s)
    row = s.exec(
        select(ProviderPricing).where(
            ProviderPricing.provider_id == "xai",
            ProviderPricing.model_id == "grok-4.3",
        )
    ).first()
    assert row is not None
    assert row.input_per_mtok == 1.25
    assert row.output_per_mtok == 2.50
    assert row.cache_read_per_mtok == 0.20
    assert row.cache_create_per_mtok == 1.25  # no write premium: bills as input
    assert row.effective_from.isoformat() == "2025-07-01"


def test_seed_xai_grok_build_rates():
    """Observed grok CLI signals.json id inherits grok-build-0.1 rates."""
    s = _make_session()
    seed_pricing_table(s)
    row = s.exec(
        select(ProviderPricing).where(
            ProviderPricing.provider_id == "xai",
            ProviderPricing.model_id == "grok-build",
        )
    ).first()
    assert row is not None
    assert row.input_per_mtok == 1.00
    assert row.output_per_mtok == 2.00
    assert row.cache_read_per_mtok == 0.20
    assert row.cache_create_per_mtok == 1.00


def test_seed_xai_unpriced_ids_have_no_row():
    """grok-4.1 / grok-4-mini get no dedicated row (issue #346).

    This only pins the seed contents — the calculator-level consequence of
    having no row differs per id (grok-4.1 stays $0, grok-4-mini falls back to
    the grok-4 family rate) and is pinned in test_cost_calculator.py.
    """
    s = _make_session()
    seed_pricing_table(s)
    for model_id in ("grok-4.1", "grok-4-mini"):
        row = s.exec(
            select(ProviderPricing).where(
                ProviderPricing.provider_id == "xai",
                ProviderPricing.model_id == model_id,
            )
        ).first()
        assert row is None, f"{model_id} should have no pricing row"
