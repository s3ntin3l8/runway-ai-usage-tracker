# app/services/webhooks.py
import logging
from datetime import UTC, datetime

import httpx
from sqlmodel import Session, select

from app.models.db import WebhookConfig
from app.models.schemas import LimitCard

logger = logging.getLogger(__name__)

_HYSTERESIS = 0.85  # reset alert when usage drops below threshold * 0.85


async def check_and_fire(cards: list[LimitCard], session: Session) -> None:
    """
    Check all active webhook configs against current card values.

    For each config:
    - Fire when ANY matched card has used_pct >= threshold and last_fired_at is None.
    - Reset last_fired_at only when ALL matched cards are below threshold * _HYSTERESIS.
    - Cards in the dead zone (hysteresis ≤ used_pct < threshold) neither fire nor reset.

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

            # Two-pass: categorise all cards before mutating state
            breaching: list[tuple[LimitCard, float]] = []
            all_recovered = True  # true until we find a card above hysteresis

            for card in matched:
                if card.used_value is None or card.limit_value is None or card.limit_value == 0:
                    continue

                used_pct = (card.used_value / card.limit_value) * 100.0

                if used_pct >= config.threshold_pct:
                    breaching.append((card, used_pct))
                    all_recovered = False
                elif used_pct >= config.threshold_pct * _HYSTERESIS:
                    # Dead zone: above hysteresis but below threshold — hold state
                    all_recovered = False

            # Reset: every card has recovered below hysteresis
            if all_recovered and config.last_fired_at is not None:
                config.last_fired_at = None
                session.add(config)
            # Fire: at least one card is breaching and no active breach recorded
            elif breaching and config.last_fired_at is None:
                card, used_pct = max(breaching, key=lambda x: x[1])
                try:
                    await _fire_webhook(client, config, card, used_pct)
                    config.last_fired_at = datetime.now(UTC)
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
        "embeds": [
            {
                "title": f"Quota Alert: {card.service_name}",
                "color": 0xED4245,
                "fields": [
                    {"name": "Provider", "value": card.provider_id or "unknown", "inline": True},
                    {
                        "name": "Account",
                        "value": card.account_label or card.account_id or "unknown",
                        "inline": True,
                    },
                    {"name": "Usage", "value": f"{used_pct:.1f}%", "inline": True},
                    {"name": "Threshold", "value": f"{threshold:.0f}%", "inline": True},
                ],
                "footer": {"text": "Runway · quota alert"},
            }
        ]
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
                    {
                        "type": "mrkdwn",
                        "text": f"*Account:* {card.account_label or card.account_id}",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Usage:* {used_pct:.1f}% (threshold: {threshold:.0f}%)",
                    },
                ],
            },
        ]
    }
