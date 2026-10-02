"""Tests for app/services/data_health/checks/alert_channels.py (#479)."""

from __future__ import annotations

from app.models.db import WebhookConfig
from app.services.data_health.base import Severity
from app.services.data_health.checks.alert_channels import AlertChannelsCheck


def _hook(session, **kw) -> None:
    session.add(
        WebhookConfig(
            **{
                "provider_id": "*",
                "threshold_pct": 90.0,
                "url": "https://discord.example.com/hook",
                "channel": "discord",
                **kw,
            }
        )
    )
    session.commit()


def test_flags_a_deployment_with_no_webhook_at_all(session):
    report = AlertChannelsCheck().detect(session)

    assert report.total_count == 1
    assert report.severity is Severity.WARN
    (group,) = report.groups
    assert group.fixable is False
    assert group.detail == {"link": "/settings/webhooks"}


def test_an_inactive_or_credential_alert_less_webhook_does_not_count(session):
    _hook(session, active=False)
    _hook(session, url="https://hooks.example.com/b", credential_alerts=False)

    assert AlertChannelsCheck().detect(session).total_count == 1


def test_one_active_webhook_with_credential_alerts_clears_it(session):
    _hook(session, active=False)
    _hook(session, url="https://hooks.example.com/b")

    report = AlertChannelsCheck().detect(session)

    assert report.total_count == 0 and report.groups == []
