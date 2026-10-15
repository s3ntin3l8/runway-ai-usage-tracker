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
    assert group.detail == {
        "never_matched": 1,
        "not_matched_recently": 0,
        "superseded": 0,
        "to_move": 0,
    }


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

    assert group.detail == {
        "never_matched": 0,
        "not_matched_recently": 1,
        "superseded": 0,
        "to_move": 0,
    }


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


def test_an_all_machines_rule_is_not_judged_while_every_machine_is_a_few_days_quiet(session):
    # Within the 7-day "known" window but past the 1-day "alive" window.
    _machine(session, "dev-01", seen_days_ago=3)
    _rule(session)

    assert _check().detect(session).total_count == 0


def test_an_all_machines_rule_is_judged_once_one_machine_is_alive(session):
    _machine(session, "dev-01", seen_days_ago=3)
    _machine(session, "mgmt")
    _rule(session)

    assert _check().detect(session).total_count == 1


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

    assert result.counts == {
        "never_matched": 1,
        "not_matched_recently": 0,
        "superseded": 0,
        "to_move": 0,
    }
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


# --- superseded rules: an xAI login re-keyed itself under a new fingerprint ----------

XAI_BASE = "path:/home/u/.local/share/opencode/auth.json"
OLD = f"{XAI_BASE}#5b5b581893f9"
NEW = f"{XAI_BASE}#33f8cb56a55b"


def _xai_rule(session, origin: str = OLD, **overrides):
    overrides.setdefault("set_at", _ago(30))
    return make_tag(
        session, provider_id="xai", credential_origin=origin, account_id="me", **overrides
    )


def _xai_source(session, origin: str = NEW, sidecar_id: str = "dev-01", **overrides):
    return make_source(
        session,
        provider_id="xai",
        account_id="me",
        source_id=f"sidecar:{sidecar_id}:{origin}",
        credential_origin=origin,
        sidecar_id=sidecar_id,
        **overrides,
    )


def test_a_rule_on_a_rotated_xai_fingerprint_is_labelled_superseded(session):
    _machine(session, "dev-01")
    _xai_rule(session, sidecar_id="dev-01")
    _xai_source(session)

    (group,) = _check().detect(session).groups

    assert group.detail == {
        "never_matched": 0,
        "not_matched_recently": 0,
        "superseded": 1,
        "to_move": 1,
    }
    (sample,) = group.samples
    assert "superseded by a newer login" in sample.label
    assert sample.detail["current_origin"] == NEW


def test_apply_moves_a_superseded_rule_onto_the_current_origin(session):
    _machine(session, "dev-01")
    rule = _xai_rule(session, sidecar_id="dev-01", set_by="operator")
    _xai_source(session)

    result, _ = _check().apply(session, "xai", {})

    assert "move 1 rule(s)" in result.summary
    session.expire_all()
    moved = session.get(CredentialTag, rule.id)
    assert moved is not None and moved.credential_origin == NEW
    # It now matches the live credential, so the check is clean.
    assert _check().detect(session).total_count == 0


def test_apply_deletes_a_superseded_rule_when_the_current_origin_is_already_covered(session):
    _machine(session, "dev-01")
    rule = _xai_rule(session, sidecar_id="dev-01")
    covering = _xai_rule(session, NEW, sidecar_id="dev-01", set_by="rotation")
    _xai_source(session)

    _check().apply(session, "xai", {})

    left = {t.id for t in session.exec(select(CredentialTag)).all()}
    assert left == {covering.id}
    assert rule.id not in left


def test_an_all_machines_superseded_rule_is_moved_when_one_machine_is_uncovered(session):
    _machine(session, "dev-01")
    _machine(session, "mgmt")
    rule = _xai_rule(session)  # all machines
    _xai_rule(session, NEW, sidecar_id="dev-01", set_by="rotation")
    _xai_source(session, sidecar_id="dev-01")
    _xai_source(session, sidecar_id="mgmt")

    _check().apply(session, "xai", {})

    session.expire_all()
    moved = session.get(CredentialTag, rule.id)
    assert moved is not None and moved.credential_origin == NEW


def test_a_non_superseding_provider_keeps_the_plain_never_matched_label(session):
    """Only providers whose one login re-keys itself are treated as superseded —
    for others a sibling fingerprint at the same path is a different key."""
    _machine(session, "dev-01")
    _rule(session, f"{ORIGIN}#5b5b581893f9", sidecar_id="dev-01")
    _source(session, f"{ORIGIN}#33f8cb56a55b")

    (group,) = _check().detect(session).groups

    assert group.detail == {
        "never_matched": 1,
        "not_matched_recently": 0,
        "superseded": 0,
        "to_move": 0,
    }


def test_a_stale_xai_rule_with_no_replacement_is_still_plainly_stale(session):
    _machine(session, "dev-01")
    _xai_rule(session, sidecar_id="dev-01")

    (group,) = _check().detect(session).groups

    assert group.detail == {
        "never_matched": 1,
        "not_matched_recently": 0,
        "superseded": 0,
        "to_move": 0,
    }


def test_preview_says_delete_not_move_when_the_new_login_is_already_covered(session):
    _machine(session, "dev-01")
    _xai_rule(session, sidecar_id="dev-01")
    _xai_rule(session, NEW, sidecar_id="dev-01", set_by="rotation")
    _xai_source(session)

    plan = _check().plan(session, "xai", {})

    assert plan.counts["to_move"] == 0
    assert plan.summary.startswith("Delete 1 assignment rule")
    assert "already covered" in plan.samples[0].label


def test_a_different_account_on_the_new_origin_does_not_swallow_the_operator_mapping(session):
    """Same scope + different account: re-keying would collide, so it is deleted.
    A broader rule for another account is overridden by moving a machine rule."""
    _machine(session, "dev-01")
    mine = _xai_rule(session, sidecar_id="dev-01")
    make_tag(
        session,
        provider_id="xai",
        credential_origin=NEW,
        account_id="someone-else",
        set_by="operator",
        set_at=_ago(1),
    )  # all machines, other account
    _xai_source(session)

    (rule,) = find_stale_rules(session, provider_id="xai")
    assert rule.row_id == mine.id and rule.action == "move"
    _check().apply(session, "xai", {})
    session.expire_all()
    assert session.get(CredentialTag, mine.id).credential_origin == NEW


def test_two_superseded_rules_converging_on_one_origin_keep_exactly_one(session):
    _machine(session, "dev-01")
    first = _xai_rule(session, sidecar_id="dev-01")
    second = _xai_rule(session, f"{XAI_BASE}#aaaaaaaaaaaa", sidecar_id="dev-01")
    _xai_source(session)

    plan = _check().plan(session, "xai", {})
    assert plan.counts["to_move"] == 1

    _check().apply(session, "xai", {})
    left = session.exec(select(CredentialTag)).all()
    assert [(t.credential_origin, t.id) for t in left] == [(NEW, min(first.id, second.id))]


def test_a_machine_rule_is_not_superseded_by_a_login_on_another_machine(session):
    _machine(session, "dev-01")
    _machine(session, "mgmt")
    _xai_rule(session, sidecar_id="dev-01")
    _xai_source(session, sidecar_id="mgmt")

    (group,) = _check().detect(session).groups

    assert group.detail["superseded"] == 0 and group.detail["never_matched"] == 1


def test_a_replacement_not_seen_within_the_window_is_ignored(session):
    _machine(session, "dev-01")
    _xai_rule(session, sidecar_id="dev-01")
    _xai_source(session, last_seen=_ago(30))

    (group,) = _check().detect(session).groups

    assert group.detail["superseded"] == 0
