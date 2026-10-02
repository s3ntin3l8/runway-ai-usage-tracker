"""Data Health `alert_channels` check — credential alerts have nowhere to go (#479).

The credential-alert machinery pages through webhook channels. With no active channel that
has credential alerts on, an expired or rejected credential (or one that keeps failing)
raises no alert at all and the first sign is a stale card. Not fixable here: it needs the
operator's webhook URL, so it only surfaces the gap and links to Settings → Alerts.
"""

from __future__ import annotations

from sqlmodel import Session, select

from app.models.db import WebhookConfig
from app.services.data_health.base import Check, CheckReport, Finding, FindingGroup, Severity

# Where the operator fixes it. One definition: the sample row and the group both carry it
# (the generic data-health UI reads samples, Home reads the check id and links itself).
WEBHOOKS_LINK = "/settings/webhooks"


class AlertChannelsCheck(Check):
    id = "alert_channels"
    title = "Credential alerts have no delivery channel"
    description = (
        "No active Discord or Slack webhook has credential alerts enabled, so an expired, "
        "rejected or failing credential will not notify anyone."
    )
    impact = "A credential can go bad and collection stop without any notification."
    recommended_action = "Add a webhook under Settings → Alerts with credential alerts on."
    severity = Severity.WARN

    def detect(self, session: Session) -> CheckReport:
        has_channel = (
            session.exec(
                select(WebhookConfig.id).where(
                    WebhookConfig.active == True,  # noqa: E712 — SQL comparison
                    WebhookConfig.credential_alerts == True,  # noqa: E712
                )
            ).first()
            is not None
        )
        if has_channel:
            return CheckReport(check_id=self.id, severity=self.severity, total_count=0, groups=[])
        group = FindingGroup(
            key="no_channel",
            label="No webhook will receive credential alerts",
            count=1,
            fixable=False,
            not_fixable_reason="add a webhook (it needs your Discord or Slack URL)",
            samples=[Finding(label="credential alerts", detail={"link": WEBHOOKS_LINK})],
            detail={"link": WEBHOOKS_LINK},
        )
        return CheckReport(check_id=self.id, severity=self.severity, total_count=1, groups=[group])
