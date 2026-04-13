# app/services/webhooks.py
import logging
import httpx
from datetime import datetime, timezone
from sqlmodel import Session, select
from app.models.db import WebhookConfig
from app.models.schemas import LimitCard
from typing import Optional

logger = logging.getLogger(__name__)

_HYSTERESIS = 0.85  # reset alert when usage drops below threshold * 0.85


async def check_and_fire(cards: list[LimitCard], session: Session) -> None:
    """
    Check all active webhook configs against current card values.
    Fires when used_pct >= threshold and last_fired_at is None.
    Resets last_fired_at when usage drops below threshold * _HYSTERESIS.
    Provider-specific configs are evaluated before global '*' configs.
    """
    configs = session.exec(
        select(WebhookConfig).where(WebhookConfig.active == True)  # noqa: E712
    ).all()
    if not configs:
        return

    # Build provider → cards lookup
    card_by_provider: dict[str, list[LimitCard]] = {}
    for card in cards:
        if card.provider_id:
            card_by_provider.setdefault(card.provider_id, []).append(card)

    # Evaluate specific providers first, wildcards last
    sorted_configs = sorted(configs, key=lambda c: (c.provider_id == "*", c.id))

    async with httpx.AsyncClient(timeout=5.0) as client:
        for config in sorted_configs:
            if config.provider_id == "*":
                matched = [c for cards_list in card_by_provider.values() for c in cards_list]
            else:
                matched = card_by_provider.get(config.provider_id, [])

            for card in matched:
                if card.used_value is None or card.limit_value is None or card.limit_value == 0:
                    continue

                used_pct = (card.used_value / card.limit_value) * 100.0

                # Reset: usage recovered below hysteresis band
                if used_pct < config.threshold_pct * _HYSTERESIS:
                    if config.last_fired_at is not None:
                        config.last_fired_at = None
                        session.add(config)
                    continue

                # Fire: threshold crossed and no active breach recorded
                if used_pct >= config.threshold_pct and config.last_fired_at is None:
                    try:
                        await _fire_webhook(client, config, card, used_pct)
                        config.last_fired_at = datetime.now(timezone.utc)
                        session.add(config)
                    except Exception as e:
                        logger.error(f"Webhook delivery failed for config {config.id}: {e}")

    session.commit()


async def _fire_webhook(
    client: httpx.AsyncClient,
    config: WebhookConfig,
    card: LimitCard,
    used_pct: float,
) -> None:
    """Dispatch a single webhook notification."""
    if config.channel == "discord":
        payload = _discord_payload(card, used_pct, config.threshold_pct)
    else:
        payload = _slack_payload(card, used_pct, config.threshold_pct)
    response = await client.post(config.url, json=payload)
    response.raise_for_status()
    logger.info(f"Webhook fired: {config.provider_id} @ {used_pct:.1f}% (config {config.id})")


def _discord_payload(card: LimitCard, used_pct: float, threshold: float) -> dict:
    return {
        "embeds": [{
            "title": f"Quota Alert: {card.service_name}",
            "color": 0xED4245,
            "fields": [
                {"name": "Provider", "value": card.provider_id or "unknown", "inline": True},
                {"name": "Account", "value": card.account_label or card.account_id or "unknown", "inline": True},
                {"name": "Usage", "value": f"{used_pct:.1f}%", "inline": True},
                {"name": "Threshold", "value": f"{threshold:.0f}%", "inline": True},
            ],
            "footer": {"text": "Runway · quota alert"},
        }]
    }


def _slack_payload(card: LimitCard, used_pct: float, threshold: float) -> dict:
    return {
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"Quota Alert: {card.service_name}"},
            },
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"*Provider:* {card.provider_id}"},
                    {"type": "mrkdwn", "text": f"*Account:* {card.account_label or card.account_id}"},
                    {"type": "mrkdwn", "text": f"*Usage:* {used_pct:.1f}% (threshold: {threshold:.0f}%)"},
                ],
            },
        ]
    }
