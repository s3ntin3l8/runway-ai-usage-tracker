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
