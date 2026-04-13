from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.models.db import WebhookConfig
from app.models.schemas import LimitCard


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _card(provider="anthropic", used=950.0, limit=1000.0, account="acc1"):
    return LimitCard(
        service_name="Test",
        icon="T",
        remaining="5%",
        unit="tokens",
        reset="monthly",
        health="warning",
        pace="high",
        detail="",
        provider_id=provider,
        account_id=account,
        account_label="test@example.com",
        used_value=used,
        limit_value=limit,
        data_source="oauth",
    )


def _config(session, provider="anthropic", threshold=90.0, channel="discord", last_fired=None):
    cfg = WebhookConfig(
        provider_id=provider,
        threshold_pct=threshold,
        url="https://discord.example.com/webhook",
        channel=channel,
        active=True,
        last_fired_at=last_fired,
    )
    session.add(cfg)
    session.commit()
    session.refresh(cfg)
    return cfg


@pytest.mark.asyncio
async def test_fires_when_above_threshold(session):
    """Webhook fires when usage exceeds threshold and last_fired_at is None."""
    from app.services.webhooks import check_and_fire

    _config(session)  # threshold=90%, last_fired=None

    with patch("app.services.webhooks.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client

        await check_and_fire([_card(used=950.0, limit=1000.0)], session)  # 95% > 90%

        assert mock_client.post.called


@pytest.mark.asyncio
async def test_does_not_fire_when_below_threshold(session):
    """No webhook fired when usage is below threshold."""
    from app.services.webhooks import check_and_fire

    _config(session)  # threshold=90%

    with patch("app.services.webhooks.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock()
        mock_cls.return_value = mock_client

        await check_and_fire([_card(used=800.0, limit=1000.0)], session)  # 80% < 90%

        assert not mock_client.post.called


@pytest.mark.asyncio
async def test_does_not_refire_same_breach(session):
    """Once fired, does not fire again while still above threshold."""
    from app.services.webhooks import check_and_fire

    _config(session, last_fired=datetime.now(UTC))  # already fired

    with patch("app.services.webhooks.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock()
        mock_cls.return_value = mock_client

        await check_and_fire([_card(used=950.0)], session)  # still above

        assert not mock_client.post.called


@pytest.mark.asyncio
async def test_resets_when_below_hysteresis(session):
    """last_fired_at cleared when usage drops below threshold * 0.85."""
    from app.services.webhooks import check_and_fire

    fired_time = datetime.now(UTC)
    cfg = _config(session, threshold=90.0, last_fired=fired_time)

    with patch("app.services.webhooks.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock()
        mock_cls.return_value = mock_client

        # 70% < 90% * 0.85 = 76.5% → should reset
        await check_and_fire([_card(used=700.0, limit=1000.0)], session)

    session.refresh(cfg)
    assert cfg.last_fired_at is None


def test_discord_payload_has_embed():
    """Discord payload contains embeds array with correct color."""
    from app.services.webhooks import _discord_payload

    card = _card()
    payload = _discord_payload(card, 95.0, 90.0)
    assert "embeds" in payload
    assert payload["embeds"][0]["color"] == 0xED4245


def test_slack_payload_has_blocks():
    """Slack payload contains blocks array."""
    from app.services.webhooks import _slack_payload

    card = _card()
    payload = _slack_payload(card, 95.0, 90.0)
    assert "blocks" in payload
    assert payload["blocks"][0]["type"] == "header"


@pytest.mark.asyncio
async def test_dead_zone_preserves_last_fired_at(session):
    """Usage in dead zone (hysteresis ≤ used_pct < threshold) neither fires nor resets."""
    from app.services.webhooks import check_and_fire

    fired_time = datetime.now(UTC)
    cfg = _config(session, threshold=90.0, last_fired=fired_time)

    with patch("app.services.webhooks.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock()
        mock_cls.return_value = mock_client

        # 80% is between 76.5% (hysteresis) and 90% (threshold) — dead zone
        await check_and_fire([_card(used=800.0, limit=1000.0)], session)

        assert not mock_client.post.called

    session.refresh(cfg)
    assert cfg.last_fired_at is not None  # not reset
    # SQLite strips tzinfo on round-trip; compare naive timestamps
    assert cfg.last_fired_at.replace(tzinfo=None) == fired_time.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_http_failure_does_not_set_last_fired_at(session):
    """A failed HTTP call logs the error and leaves last_fired_at as None."""
    import httpx as _httpx

    from app.services.webhooks import check_and_fire

    cfg = _config(session)  # threshold=90%, last_fired=None

    with patch("app.services.webhooks.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock(
            side_effect=_httpx.HTTPStatusError("500", request=MagicMock(), response=MagicMock())
        )
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client

        await check_and_fire([_card(used=950.0, limit=1000.0)], session)

    session.refresh(cfg)
    assert cfg.last_fired_at is None  # not set on failure


@pytest.mark.asyncio
async def test_skips_cards_with_none_or_zero_limit(session):
    """Cards with None or zero limit_value are safely skipped."""
    from app.services.webhooks import check_and_fire

    _config(session)

    with patch("app.services.webhooks.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock()
        mock_cls.return_value = mock_client

        none_limit_card = _card(used=950.0, limit=1000.0)
        none_limit_card = LimitCard(**{**none_limit_card.model_dump(), "limit_value": None})
        zero_limit_card = _card(used=950.0, limit=1000.0)
        zero_limit_card = LimitCard(**{**zero_limit_card.model_dump(), "limit_value": 0.0})

        await check_and_fire([none_limit_card, zero_limit_card], session)

        assert not mock_client.post.called


@pytest.mark.asyncio
async def test_global_wildcard_matches_all_providers(session):
    """provider_id='*' config fires for any provider card."""
    from app.services.webhooks import check_and_fire

    cfg = WebhookConfig(
        provider_id="*",
        threshold_pct=90.0,
        url="https://discord.example.com/webhook",
        channel="discord",
        active=True,
        last_fired_at=None,
    )
    session.add(cfg)
    session.commit()

    with patch("app.services.webhooks.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client

        await check_and_fire([_card(provider="openai", used=950.0)], session)

        assert mock_client.post.called
