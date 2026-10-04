"""Tests for app/services/data_health/checks/stale_credential_rules.py."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import select

from app.models.db import CredentialTag, SidecarRegistry
from app.services.data_health.checks.stale_credential_rules import StaleCredentialRulesCheck
from app.services.maintenance.stale_credential_rules import find_stale_rules, rule_usage
from tests.unit.data_health.conftest import make_source, make_tag

ORIGIN = "path:/home/u/.config/gh/hosts.yml"


def _check() -> StaleCredentialRulesCheck:
    return StaleCredentialRulesCheck()


def _ago(days: float) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


def _machine(session, sidecar_id: str, *, seen_days_ago: float = 0.0) -> None:
    session.add(SidecarRegistry(sidecar_id=sidecar_id, last_seen=_ago(seen_days_ago)))
    session.commit()


def _rule(session, origin: str = ORIGIN, **overrides):
    overrides.setdefault("set_at", _ago(30))
    return make_tag(
        session,
        provider_id="github",
        credential_origin=origin,
        account_id="me",
        **overrides,
    )


def _source(session, origin: str = ORIGIN, **overrides):
    overrides.setdefault("credential_origin", origin)
    overrides.setdefault("source_id", f"sidecar:{overrides.get('sidecar_id', 'dev-01')}:{origin}")
    return make_source(session, provider_id="github", account_id="me", **overrides)


def test_a_machine_rule_with_no_matching_source_is_stale(session):
    _machine(session, "dev-01")
    _rule(session, sidecar_id="dev-01")

    (group,) = _check().detect(session).groups

    assert group.key == "github"
    assert group.detail == {"never_matched": 1, "not_matched_recently": 0}


def test_a_rule_that_a_recent_source_matches_is_kept(session):
    _machine(session, "dev-01")
    _rule(session, sidecar_id="dev-01")
    _source(session)

    assert _check().detect(session).total_count == 0


def test_an_all_machines_rule_is_kept_by_any_machines_source(session):
    _machine(session, "dev-01")
    _machine(session, "mgmt")
    _rule(session)
    _source(session, sidecar_id="mgmt")

    assert _check().detect(session).total_count == 0


def test_a_plain_origin_rule_is_matched_by_a_fingerprinted_source(session):
    _machine(session, "dev-01")
    _rule(session, "env:ZAI_API_KEY")
    _source(session, "env:ZAI_API_KEY#abc123def456")

    assert _check().detect(session).total_count == 0


def test_an_all_machines_rule_shadowed_by_machine_rules_everywhere_is_unused(session):
    _machine(session, "dev-01")
    shadowed = _rule(session)
    scoped = _rule(session, sidecar_id="dev-01")
    _source(session)

    stale = find_stale_rules(session)

    assert [r.row_id for r in stale] == [shadowed.id]
    assert scoped.id in rule_usage(session)


def test_a_rule_last_matched_before_the_window_is_stale_with_its_last_match(session):
    _machine(session, "dev-01")
    _rule(session, sidecar_id="dev-01")
    _source(session, last_seen=_ago(20))

    (group,) = _check().detect(session).groups

    assert group.detail == {"never_matched": 0, "not_matched_recently": 1}


@pytest.mark.parametrize(
    "overrides",
    [
        {"set_by": "identity_claim"},
        {"set_by": "identity_verification"},
        {"set_by": "rotation"},
        {"target_provider_id": "antigravity"},
        {"set_at": _ago(2)},
    ],
)
def test_automatic_redirect_and_new_rules_are_never_listed(session, overrides):
    _machine(session, "dev-01")
    _rule(session, sidecar_id="dev-01", **overrides)

    assert _check().detect(session).total_count == 0


@pytest.mark.parametrize("origin", ["provider:github", "config:github:me"])
def test_provider_fallback_and_config_origins_are_never_listed(session, origin):
    _machine(session, "dev-01")
    _rule(session, origin, sidecar_id="dev-01")

    assert _check().detect(session).total_count == 0


def test_a_machine_rule_is_not_judged_while_its_machine_is_offline(session):
    _machine(session, "laptop", seen_days_ago=3)
    _machine(session, "dev-01")
    _rule(session, sidecar_id="laptop")

    assert _check().detect(session).total_count == 0


def test_a_machine_rule_for_an_unknown_machine_is_not_judged(session):
    _machine(session, "dev-01")
    _rule(session, sidecar_id="ghost")

    assert _check().detect(session).total_count == 0


def test_nothing_is_judged_while_no_machine_is_reporting(session):
    _rule(session)

    assert _check().detect(session).total_count == 0


def test_apply_deletes_only_what_the_plan_reported(session):
    _machine(session, "dev-01")
    stale = _rule(session, sidecar_id="dev-01")
    kept = _rule(session, "path:/other", sidecar_id="dev-01")
    _source(session, "path:/other")

    result, _ = _check().apply(session, "github", {})

    assert result.counts == {"never_matched": 1, "not_matched_recently": 0}
    left = {t.id for t in session.exec(select(CredentialTag)).all()}
    assert left == {kept.id}
    assert stale.id not in left


def test_plan_raises_when_nothing_is_stale(session):
    with pytest.raises(ValueError, match="No stale assignment rules"):
        _check().plan(session, "github", {})


def test_samples_carry_no_credential_values(session):
    _machine(session, "dev-01")
    _rule(session, sidecar_id="dev-01")

    plan = _check().plan(session, "github", {})

    (sample,) = plan.samples
    assert set(sample.detail) == {
        "provider_id",
        "credential_origin",
        "sidecar_id",
        "last_matched_at",
    }
