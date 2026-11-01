"""Tests for app/services/data_health/checks/openrouter_model_events.py and
app/services/maintenance/openrouter_retag.py."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlmodel import select

from app.models.db import UsageEvent, UsagePeriodRollup
from app.services.data_health.checks.openrouter_model_events import OpenRouterModelEventsCheck
from app.services.maintenance.openrouter_retag import (
    NoTargetAccountError,
    plan_openrouter_retag,
)
from app.services.model_normalization import is_openrouter_model_id
from tests.unit.data_health.conftest import make_config, make_event

ME = "me@example.com"


def _check() -> OpenRouterModelEventsCheck:
    return OpenRouterModelEventsCheck()


def _target(session, account: str = ME) -> None:
    make_config(session, provider_id="openrouter", account_id=account)


def _ev(session, event_id, provider_id, model_id="tencent/hy3:free", **kw):
    kw.setdefault("account_id", "default" if provider_id == "opencode-byok" else ME)
    return make_event(session, event_id=event_id, provider_id=provider_id, model_id=model_id, **kw)


def _raw_ev(session, event_id, provider_id, model_id="tencent/hy3:free", **kw):
    """A row whose event id is exactly what was given.

    `make_event` prefixes every id with "msg_", which would hide the
    `hermes|` event-id marker the check keys off — the durable marker for
    Hermes rows whose `source_app` was backfilled to their provider id.
    """
    kw.setdefault("account_id", ME)
    kw.setdefault("sidecar_id", "dev-01")
    kw.setdefault("kind", "message")
    kw.setdefault("ts", datetime(2026, 9, 1, tzinfo=UTC))
    kw.setdefault("tokens_input", 100)
    kw.setdefault("tokens_output", 20)
    kw.setdefault("cost_usd", 0.05)
    kw.setdefault("attribution_source", "default")
    ev = UsageEvent(event_id=event_id, provider_id=provider_id, model_id=model_id, **kw)
    session.add(ev)
    session.commit()
    return ev


def _events(session):
    return {(e.provider_id, e.event_id): e for e in session.exec(select(UsageEvent)).all()}


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("tencent/hy3:free", True),
        ("qwen/qwen3.6-plus:free", True),
        ("openrouter/free", True),
        ("OpenRouter/Auto", True),
        ("qwen/qwen3-coder-30b", False),  # vendor-prefixed local model
        ("deepseek-v4-flash-free", False),  # Zen free tier, no vendor prefix
        ("mimo-v2.5-free", False),
        ("gpt-5.4", False),
        ("", False),
        (None, False),
    ],
)
def test_predicate(model_id, expected):
    assert is_openrouter_model_id(model_id) is expected


def test_sql_clause_agrees_with_the_python_predicate(session):
    models = ["tencent/hy3:free", "openrouter/free", "qwen/qwen3-coder-30b", "gpt-5.4", "x-free"]
    for i, model in enumerate(models):
        _ev(session, str(i), "opencode", model)

    report = _check().detect(session)

    moved = {s.detail["model_id"] for g in report.groups for s in g.samples}
    assert moved == {m for m in models if is_openrouter_model_id(m)}


def test_detect_groups_by_source_provider_and_ignores_other_events(session):
    _target(session)
    _ev(session, "1", "opencode")
    _ev(session, "2", "opencode-byok")
    _ev(session, "3", "opencode", "qwen/qwen3-coder-30b")
    _ev(session, "4", "openrouter")  # already correct

    report = _check().detect(session)

    assert {g.key: g.count for g in report.groups} == {"opencode": 1, "opencode-byok": 1}
    assert all(g.fixable and g.detail["target_account"] == ME for g in report.groups)


def test_group_is_not_fixable_without_a_single_target_account(session):
    _ev(session, "1", "opencode-byok")
    (group,) = _check().detect(session).groups
    assert not group.fixable

    _target(session, "a")
    _target(session, "b")
    (group,) = _check().detect(session).groups
    assert not group.fixable
    with pytest.raises(NoTargetAccountError):
        _check().plan(session, "opencode-byok", {})
    with pytest.raises(NoTargetAccountError):
        _check().apply(session, "opencode-byok", {})
    assert {e.provider_id for e in session.exec(select(UsageEvent)).all()} == {"opencode-byok"}


def test_plan_is_read_only_and_counts_collisions_with_openrouter(session):
    _target(session)
    _ev(session, "dup", "opencode")
    _ev(session, "dup", "opencode-byok")
    _ev(session, "only", "opencode-byok")

    plan = _check().plan(session, "opencode-byok", {})

    assert plan.counts == {"total": 2, "duplicates": 0, "to_move": 2}
    assert len(session.exec(select(UsageEvent)).all()) == 3


def test_applying_each_group_in_turn_collapses_byok_duplicates_of_opencode_rows(session):
    _target(session)
    _ev(session, "dup", "opencode", tokens_input=500)
    _ev(session, "dup", "opencode-byok", tokens_input=10)  # same message, thinner copy
    _ev(session, "byok-only", "opencode-byok")

    first, _ = _check().apply(session, "opencode-byok", {})
    assert first.counts["duplicates_resolved"] == 0
    assert set(_events(session)) == {
        ("opencode", "msg_dup"),
        ("openrouter", "msg_dup"),
        ("openrouter", "msg_byok-only"),
    }

    # The preview for the second group is exact: one collision, one copy dropped.
    assert _check().plan(session, "opencode", {}).counts["duplicates"] == 1
    second, _ = _check().apply(session, "opencode", {})

    events = _events(session)
    assert second.counts["duplicates_resolved"] == 1
    assert set(events) == {("openrouter", "msg_dup"), ("openrouter", "msg_byok-only")}
    assert events[("openrouter", "msg_dup")].tokens_input == 500  # the richer copy won
    assert {e.account_id for e in events.values()} == {ME}
    assert {e.attribution_source for e in events.values()} == {"tag"}
    assert _check().detect(session).total_count == 0


def test_apply_resolves_a_collision_with_an_existing_openrouter_row(session):
    _target(session)
    _ev(session, "same", "opencode", tokens_input=900)
    _ev(session, "same", "openrouter", tokens_input=5)

    _check().apply(session, "opencode", {})

    (only,) = session.exec(select(UsageEvent)).all()
    assert (only.provider_id, only.tokens_input) == ("openrouter", 900)


def test_apply_leaves_other_events_alone(session):
    _target(session)
    _ev(session, "1", "opencode")
    _ev(session, "2", "opencode", "qwen/qwen3-coder-30b")
    _ev(session, "3", "opencode-free", "mimo-v2.5-free")

    _check().apply(session, "opencode", {})

    assert set(_events(session)) == {
        ("openrouter", "msg_1"),
        ("opencode", "msg_2"),
        ("opencode-free", "msg_3"),
    }


def test_apply_preserves_a_source_reported_cost(session):
    _target(session)
    _ev(session, "1", "opencode", cost_usd=0.42)

    _check().apply(session, "opencode", {})

    (ev,) = session.exec(select(UsageEvent)).all()
    assert ev.cost_reported_usd == pytest.approx(0.42)


def test_apply_rebuilds_rollups_for_the_emptied_and_the_target_pair(session):
    _target(session)
    _ev(session, "1", "opencode-byok", ts=datetime(2026, 9, 1, tzinfo=UTC))

    _check().apply(session, "opencode-byok", {})

    rollups = session.exec(select(UsagePeriodRollup)).all()
    assert {r.provider_id for r in rollups} == {"openrouter"}
    assert {r.account_id for r in rollups} == {ME}


def test_apply_duplicate_loser_pair_rollups_are_rebuilt_too(session):
    """The copy under `opencode` loses; its pair must not keep stale rollups."""
    _target(session)
    _ev(session, "dup", "opencode", tokens_input=1)
    _ev(session, "dup", "opencode-byok", tokens_input=500)

    _check().apply(session, "opencode-byok", {})
    _check().apply(session, "opencode", {})

    assert {r.provider_id for r in session.exec(select(UsagePeriodRollup)).all()} == {"openrouter"}


#: The model the Hermes extractor files under OpenCode Zen.
HERMES_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"


@pytest.mark.parametrize(
    ("source_app", "event_id"),
    [
        # A current Hermes row: the extractor stamps source_app="hermes".
        ("hermes", "hermes|review-bot|api-1|nvidia/nemotron-3-ultra-550b-a55b:free|title|c1s1"),
        # A legacy row collected before source_app existed: the migration
        # backfilled source_app to the provider id, so only the `hermes|`
        # event-id prefix still identifies it as Hermes.
        (
            "opencode-free",
            "hermes|review-bot|api-2|nvidia/nemotron-3-ultra-550b-a55b:free|title|c1s1",
        ),
        # The same for a row that predates the backfill entirely.
        (None, "hermes|review-bot|api-3|nvidia/nemotron-3-ultra-550b-a55b:free|title|c1s1"),
        # A Hermes row whose id does not carry the `hermes|` prefix: only
        # `source_app` identifies it, so the clause has to stand on its own.
        ("hermes", "msg_hermes-without-the-prefix"),
    ],
)
def test_hermes_opencode_free_event_is_not_detected_planned_or_moved(session, source_app, event_id):
    """#1126: Hermes reaches OpenCode Zen directly, so its traffic is not
    OpenRouter usage — even though the model id has the `vendor/model:free`
    shape the check keys off.

    All four shapes must be excluded: the current one (`source_app` set to
    `hermes`), the backfilled one (`source_app` rewritten to the provider id
    by `_backfill_source_app`), the pre-backfill one (`source_app` still
    NULL), and one identified by `source_app` alone. The middle two are only
    recognisable by the durable `hermes|` event-id prefix; the last one only
    by `source_app`, so neither marker can be dropped.
    """
    _target(session)
    _raw_ev(
        session,
        event_id,
        "opencode-free",
        model_id=HERMES_MODEL,
        source_app=source_app,
    )

    report = _check().detect(session)
    assert [g.key for g in report.groups] == []

    plan = plan_openrouter_retag(session, "opencode-free")
    assert plan.total == 0

    result, _ = _check().apply(session, "opencode-free", {})
    assert result.counts["retagged"] == 0

    (ev,) = session.exec(select(UsageEvent)).all()
    assert ev.provider_id == "opencode-free"
    assert ev.attribution_source == "default"


def test_hermes_event_under_opencode_is_not_detected(session):
    """#1126: the same exclusion applies under the `opencode` provider."""
    _target(session)
    _raw_ev(
        session,
        "hermes|review-bot|api-4|nvidia/nemotron-3-ultra-550b-a55b:free|title|c1s1",
        "opencode",
        model_id=HERMES_MODEL,
        source_app="hermes",
    )

    report = _check().detect(session)
    assert [g.key for g in report.groups] == []
    assert plan_openrouter_retag(session, "opencode").total == 0


def test_opencode_vendor_free_model_is_still_detected_and_moved(session):
    """#1126: the genuine OpenRouter traffic must keep moving.

    An OpenCode event whose model id only OpenRouter serves is filed under
    `opencode` by an old sidecar mapping. It is not Hermes traffic, so the
    check still flags it and the fix still moves it.
    """
    _target(session)
    _raw_ev(session, "genuine-openrouter", "opencode", model_id="vendor/x:free")

    report = _check().detect(session)
    assert [(g.key, g.count) for g in report.groups] == [("opencode", 1)]

    result, _ = _check().apply(session, "opencode", {})
    assert result.counts["retagged"] == 1
    (ev,) = session.exec(select(UsageEvent)).all()
    assert ev.provider_id == "openrouter"
    assert ev.account_id == ME


def test_hermes_row_does_not_hide_a_genuine_row_under_the_same_provider(session):
    """#1126: excluding Hermes rows must not drop the provider's real findings.

    A Hermes row and a genuine `vendor/model:free` row under `opencode`
    together: only the genuine one is counted, and only it is moved. An
    implementation that discarded the whole group on seeing a Hermes row
    would report nothing and move nothing.
    """
    _target(session)
    _raw_ev(
        session,
        "hermes|review-bot|api-5|nvidia/nemotron-3-ultra-550b-a55b:free|title|c1s1",
        "opencode",
        model_id=HERMES_MODEL,
        source_app="hermes",
    )
    _raw_ev(session, "genuine-openrouter", "opencode", model_id="vendor/x:free")

    report = _check().detect(session)
    assert [(g.key, g.count) for g in report.groups] == [("opencode", 1)]
    assert [s.label for s in report.groups[0].samples] == ["opencode/vendor/x:free"]

    result, _ = _check().apply(session, "opencode", {})
    assert result.counts["retagged"] == 1
    moved = {(e.provider_id, e.event_id) for e in session.exec(select(UsageEvent)).all()}
    assert moved == {
        ("opencode", "hermes|review-bot|api-5|nvidia/nemotron-3-ultra-550b-a55b:free|title|c1s1"),
        ("openrouter", "genuine-openrouter"),
    }


def test_raw_provider_openrouter_is_flagged_whatever_the_model_id(session):
    """#1122: the stored raw providerID is the gateway tag, so an `openrouter`
    row is OpenRouter traffic even when the model id has no vendor prefix."""
    _target(session)
    _raw_ev(
        session, "raw-openrouter", "opencode", model_id="hy3-free", raw_provider_id="openrouter"
    )

    report = _check().detect(session)
    assert [(g.key, g.count) for g in report.groups] == [("opencode", 1)]


@pytest.mark.parametrize(
    "raw_provider_id",
    ["opencode", "opencode-go", "open-design-byok"],
)
def test_raw_provider_opencode_is_not_flagged_whatever_the_model_id(session, raw_provider_id):
    """#1122: a Zen (`opencode`) row is not OpenRouter traffic, even when the
    model id has the `vendor/model:free` shape the check used to guess from.

    The set is the one `docs/data-health.md` names: `opencode`,
    `opencode-go` and `open-design-byok` are the gateway tags that are not
    OpenRouter.
    """
    _target(session)
    _raw_ev(
        session,
        "raw-opencode",
        "opencode",
        model_id="vendor/x:free",
        raw_provider_id=raw_provider_id,
    )

    report = _check().detect(session)
    assert [g.key for g in report.groups] == []


@pytest.mark.parametrize(
    ("raw_provider_id", "expected"),
    [
        # Absent or blank: fall back to the model-id shape test, which flags
        # a `vendor/model:free` id.
        (None, 1),
        ("", 1),
        ("   ", 1),
    ],
)
def test_raw_provider_falls_back_to_the_model_id_test_when_absent_or_blank(
    session, raw_provider_id, expected
):
    """#1122: a missing or blank raw provider must not silently drop the event
    out of the check — it falls back to today's model-id shape test."""
    _target(session)
    _raw_ev(
        session,
        "fallback",
        "opencode",
        model_id="vendor/x:free",
        raw_provider_id=raw_provider_id,
    )

    report = _check().detect(session)
    assert sum(g.count for g in report.groups) == expected


@pytest.mark.parametrize(
    "raw_provider_id",
    ["OpenRouter", " OpenRouter "],
)
def test_raw_provider_is_matched_case_insensitively_and_trimmed(session, raw_provider_id):
    """#1122: the stored raw providerID is normalised before it is compared —
    a differently cased or padded "OpenRouter" still identifies the gateway.

    The model id here has no vendor prefix, so the model-id shape test would
    not flag it: only the raw provider can.
    """
    _target(session)
    _raw_ev(
        session,
        "raw-openrouter-cased",
        "opencode",
        model_id="hy3-free",
        raw_provider_id=raw_provider_id,
    )

    report = _check().detect(session)
    assert [(g.key, g.count) for g in report.groups] == [("opencode", 1)]
