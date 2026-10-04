"""The fixed list of Data Health checks, in dependency order. Ordering here
is cosmetic for iteration (a scan just runs them all); `Check.blocked_by`
is what actually enforces "fix config_default_keyed before legacy_provider_ids
findings feed unpriced_models" — see `jobs.py`, which computes each report's
`blocked_by` from the *other* checks' live findings before an apply is
allowed to run.
"""

from __future__ import annotations

from app.services.data_health.base import Check
from app.services.data_health.checks.alert_channels import AlertChannelsCheck
from app.services.data_health.checks.config_default_keyed import ConfigDefaultKeyedCheck
from app.services.data_health.checks.legacy_provider_ids import LegacyProviderIdsCheck
from app.services.data_health.checks.lone_default_events import LoneDefaultEventsCheck
from app.services.data_health.checks.misidentified_gauge_series import (
    MisidentifiedGaugeSeriesCheck,
)
from app.services.data_health.checks.orphan_credential_sources import OrphanCredentialSourcesCheck
from app.services.data_health.checks.orphan_credential_tags import OrphanCredentialTagsCheck
from app.services.data_health.checks.orphan_gauge_series import OrphanGaugeSeriesCheck
from app.services.data_health.checks.pending_events import PendingEventsCheck
from app.services.data_health.checks.rollup_drift import RollupDriftCheck
from app.services.data_health.checks.stale_credential_rules import StaleCredentialRulesCheck
from app.services.data_health.checks.stale_credential_sources import StaleCredentialSourcesCheck
from app.services.data_health.checks.unpriced_models import UnpricedModelsCheck

REGISTRY: list[Check] = [
    ConfigDefaultKeyedCheck(),
    LegacyProviderIdsCheck(),
    LoneDefaultEventsCheck(),
    OrphanCredentialTagsCheck(),
    OrphanCredentialSourcesCheck(),
    StaleCredentialSourcesCheck(),
    StaleCredentialRulesCheck(),
    OrphanGaugeSeriesCheck(),
    MisidentifiedGaugeSeriesCheck(),
    UnpricedModelsCheck(),
    RollupDriftCheck(),
    PendingEventsCheck(),
    AlertChannelsCheck(),
]

BY_ID: dict[str, Check] = {check.id: check for check in REGISTRY}


def get_check(check_id: str) -> Check:
    try:
        return BY_ID[check_id]
    except KeyError:
        raise KeyError(f"unknown data health check id: {check_id!r}") from None
