from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.core.db import SQLITE_CONNECT_ARGS, configure_sqlite_engine
from app.models.db import (
    CredentialSource,
    CredentialTag,
    PendingUsageEvent,
    ProviderConfig,
    UsageEvent,
    UsagePeriodRollup,
)
from app.models.schemas import UsageEventPush
from app.services.event_ingestor import EventIngestor
from app.services.pricing_seed import seed_pricing_table


def _seeded_session():
    engine = create_engine("sqlite://", connect_args=SQLITE_CONNECT_ARGS, poolclass=StaticPool)
    configure_sqlite_engine(engine)
    SQLModel.metadata.create_all(engine)
    s = Session(engine)
    seed_pricing_table(s)
    return s


def _make_push(event_id="msg_1", ts="2026-05-08T14:23:11+00:00", **kw):
    base = {
        "provider_id": "anthropic",
        "account_id": "user@x.com",
        "event_id": event_id,
        "ts": ts,
        "model_id": "sonnet",
        "tokens_input": 100,
        "tokens_output": 200,
    }
    base.update(kw)
    return UsageEventPush(**base)


def test_ingest_single_event_writes_event_row():
    s = _seeded_session()
    res = EventIngestor(s).ingest([_make_push()], sidecar_id="dev-01")
    assert res.events_inserted == 1
    assert res.events_duplicate == 0
    rows = s.exec(select(UsageEvent)).all()
    assert len(rows) == 1
    assert rows[0].sidecar_id == "dev-01"
    assert rows[0].cost_usd > 0


def test_ingest_duplicate_event_is_noop():
    s = _seeded_session()
    push = _make_push()
    EventIngestor(s).ingest([push], sidecar_id="dev-01")
    res2 = EventIngestor(s).ingest([push], sidecar_id="dev-01")
    assert res2.events_inserted == 0
    assert res2.events_duplicate == 1
    assert len(s.exec(select(UsageEvent)).all()) == 1


def test_ingest_updates_rollups():
    s = _seeded_session()
    EventIngestor(s).ingest(
        [_make_push("msg_1"), _make_push("msg_2")],
        sidecar_id="dev-01",
    )
    day_row = s.exec(
        select(UsagePeriodRollup).where(
            UsagePeriodRollup.period_type == "day",
            UsagePeriodRollup.period_key == "2026-05-08",
            UsagePeriodRollup.model_id == "",
            UsagePeriodRollup.sidecar_id == "",
        )
    ).first()
    assert day_row.msgs == 2
    assert day_row.tokens_input == 200


def test_ingest_derives_project_and_persists_context():
    """cwd flows through; project is the server-derived basename; tools/latency persist."""
    s = _seeded_session()
    EventIngestor(s).ingest(
        [
            _make_push(
                cwd="/home/user/projects/runway/",
                git_branch="main",
                tool_names=["Read", "Bash"],
                latency_ms=1234,
            )
        ],
        sidecar_id="dev-01",
    )
    row = s.exec(select(UsageEvent)).first()
    assert row.cwd == "/home/user/projects/runway/"
    assert row.project == "runway"  # basename, trailing slash stripped
    assert row.git_branch == "main"
    assert row.tools_json == '["Read", "Bash"]'
    assert row.latency_ms == 1234


def test_ingest_applies_the_configured_worktree_patterns():
    import json

    from app.models.db import SystemConfig

    s = _seeded_session()
    s.add(SystemConfig(worktree_patterns_json=json.dumps(["*-wt"])))
    s.commit()
    EventIngestor(s).ingest([_make_push(cwd="/home/u/repo-wt/slug/app")], sidecar_id="dev-01")
    row = s.exec(select(UsageEvent)).first()
    assert row.cwd == "/home/u/repo-wt/slug/app"
    assert row.project == "repo"


def test_ingest_without_cwd_leaves_project_null():
    s = _seeded_session()
    EventIngestor(s).ingest([_make_push()], sidecar_id="dev-01")
    row = s.exec(select(UsageEvent)).first()
    assert row.cwd is None
    assert row.project is None
    assert row.tools_json is None


def test_ingest_prefers_the_reported_repository_over_the_folder_name():
    """The client resolves the repository on its own host; that name wins, so
    two checkouts of one repository group together."""
    s = _seeded_session()
    EventIngestor(s).ingest(
        [
            _make_push(
                cwd=r"C:\Users\u\Projects\ai-usage-tracker",
                project_ref="runway-ai-usage-tracker",
            )
        ],
        sidecar_id="dev-01",
    )
    row = s.exec(select(UsageEvent)).first()
    assert row.project == "runway-ai-usage-tracker"
    assert row.project_ref == "runway-ai-usage-tracker"


def test_ingest_without_a_reported_repository_falls_back_to_the_folder_name():
    s = _seeded_session()
    EventIngestor(s).ingest(
        [_make_push(cwd=r"D:\Projects\runway-ai-usage-tracker")],
        sidecar_id="dev-01",
    )
    row = s.exec(select(UsageEvent)).first()
    assert row.project == "runway-ai-usage-tracker"
    assert row.project_ref is None


def test_ingest_cleans_the_reported_repository_name():
    """The reported name ends up in the indexed, UI-facing label, so the server
    bounds and cleans it as well — the invariant must hold for any client."""
    s = _seeded_session()
    EventIngestor(s).ingest(
        [_make_push(cwd="/home/u/repo", project_ref="re\npo\x00" + "x" * 200)],
        sidecar_id="dev-01",
    )
    row = s.exec(select(UsageEvent)).first()
    assert row.project_ref is not None
    assert row.project_ref == "repo" + "x" * 124
    assert row.project == row.project_ref


def test_ingest_ignores_a_reported_path():
    """A repository *name* never holds a separator: a client reporting a path
    would pin a path-shaped label, and the two repairs would fight over the
    same rows."""
    s = _seeded_session()
    EventIngestor(s).ingest(
        [_make_push(cwd="/home/u/repo", project_ref=r"D:\repo")],
        sidecar_id="dev-01",
    )
    row = s.exec(select(UsageEvent)).first()
    assert row.project_ref is None
    assert row.project == "repo"


def test_ingest_uses_provided_cost_when_set():
    """Pay-as-you-go accounts display a provider-reported cost when present."""
    s = _seeded_session()
    s.add(
        ProviderConfig(
            provider_id="opencode", account_id="user@x.com", billing_type="pay_as_you_go"
        )
    )
    s.commit()
    # Provide an explicit cost for an opencode event with no pricing row seeded
    push = _make_push(
        event_id="oc_001",
        provider_id="opencode",
        model_id="gpt-4o",
        cost_usd=0.0088,
    )
    res = EventIngestor(s).ingest([push], sidecar_id="dev-01")
    assert res.events_inserted == 1
    row = s.exec(select(UsageEvent).where(UsageEvent.event_id == "oc_001")).first()
    assert row is not None
    assert abs(row.cost_usd - 0.0088) < 1e-9
    assert abs(row.cost_reported_usd - 0.0088) < 1e-9
    assert row.cost_estimated_usd == 0


def test_ingest_computes_cost_when_not_set():
    """When push.cost_usd is None (default), cost is computed from the pricing table."""
    s = _seeded_session()
    # sonnet has a pricing row seeded; cost should be > 0
    push = _make_push(event_id="msg_compute", cost_usd=None)
    EventIngestor(s).ingest([push], sidecar_id="dev-01")
    row = s.exec(select(UsageEvent).where(UsageEvent.event_id == "msg_compute")).first()
    assert row is not None
    # input=100 @ $3/Mtok + output=200 @ $15/Mtok = $0.0003 + $0.003 = $0.0033
    assert row.cost_usd > 0


def test_ingest_stores_cost_components_and_rolls_them_up():
    """The per-component cost columns are written on the event and summed into
    the rollup, alongside the existing total."""
    s = _seeded_session()
    push = _make_push(
        event_id="msg_cache",
        tokens_cache_read=1_000_000,  # sonnet cache_read $0.30/MT
        tokens_cache_create=1_000_000,  # sonnet cache_create $3.75/MT
    )
    EventIngestor(s).ingest([push], sidecar_id="dev-01")

    ev = s.exec(select(UsageEvent).where(UsageEvent.event_id == "msg_cache")).first()
    assert ev.cost_cache_read == 0.30
    assert ev.cost_cache_create == 3.75
    assert ev.cost_input > 0 and ev.cost_output > 0

    day = s.exec(
        select(UsagePeriodRollup).where(
            UsagePeriodRollup.period_type == "day",
            UsagePeriodRollup.model_id == "",
            UsagePeriodRollup.sidecar_id == "",
        )
    ).first()
    assert day.cost_cache_read == ev.cost_cache_read
    assert day.cost_cache_create == ev.cost_cache_create


def test_ingest_stores_components_even_with_authoritative_total():
    """A provider-supplied cost_usd stays authoritative, but pricing-derived
    components are still stored (best-effort) when a pricing row exists."""
    s = _seeded_session()
    s.add(
        ProviderConfig(
            provider_id="anthropic", account_id="user@x.com", billing_type="pay_as_you_go"
        )
    )
    s.commit()
    push = _make_push(event_id="auth_1", tokens_cache_read=1_000_000, cost_usd=99.0)
    EventIngestor(s).ingest([push], sidecar_id="dev-01")
    ev = s.exec(select(UsageEvent).where(UsageEvent.event_id == "auth_1")).first()
    assert ev.cost_usd == 99.0  # total untouched
    assert ev.cost_reported_usd == 99.0
    assert ev.cost_estimated_usd > 0
    assert ev.cost_cache_read == 0.30  # component still computed from pricing


def test_subscription_uses_calculated_usage_value_and_keeps_reported_cost():
    s = _seeded_session()
    s.add(
        ProviderConfig(
            provider_id="anthropic", account_id="user@x.com", billing_type="subscription"
        )
    )
    s.commit()
    EventIngestor(s).ingest(
        [_make_push(event_id="subscription", cost_usd=0.0)], sidecar_id="dev-01"
    )
    event = s.exec(select(UsageEvent).where(UsageEvent.event_id == "subscription")).first()
    assert event.cost_usd == event.cost_estimated_usd
    assert event.cost_usd > 0
    assert event.cost_reported_usd == 0.0


def test_unresolved_default_event_is_durable_and_excluded_from_usage_rollups():
    s = _seeded_session()
    push = _make_push(event_id="waiting", account_id="default", account_source="default")
    EventIngestor(s).ingest([push], sidecar_id="dev-01")
    assert s.exec(select(UsageEvent)).all() == []
    pending = s.exec(select(PendingUsageEvent)).first()
    assert pending is not None
    assert pending.event_id == "waiting"
    assert pending.sidecar_id == "dev-01"
    assert s.exec(select(UsagePeriodRollup)).all() == []


def test_provider_mapping_attributes_identityless_events_for_direct_and_canonical_providers():
    s = _seeded_session()
    for provider_id in ("anthropic", "minimax", "kimi_coding"):
        s.add(
            CredentialTag(
                provider_id=provider_id,
                credential_origin=f"provider:{provider_id}",
                account_id="Alice@Example.com",
                sidecar_id="host-a",
                set_by="operator",
            )
        )
    s.commit()

    pushes = [
        _make_push(
            event_id=f"{provider_id}-mapped",
            provider_id=provider_id,
            account_id="default",
            account_source="default",
        )
        for provider_id in ("anthropic", "minimax", "kimi_coding")
    ]
    EventIngestor(s).ingest(pushes, sidecar_id="host-a")

    rows = s.exec(select(UsageEvent)).all()
    assert {
        row.provider_id: (row.account_id, row.attribution_source) for row in rows
    } == dict.fromkeys(("anthropic", "minimax", "kimi_coding"), ("alice@example.com", "tag"))
    assert s.exec(select(PendingUsageEvent)).all() == []


def test_provider_mapping_is_host_scoped_and_native_identity_is_preserved():
    s = _seeded_session()
    s.add(
        CredentialTag(
            provider_id="anthropic",
            credential_origin="provider:anthropic",
            account_id="mapped@example.com",
            sidecar_id="host-a",
            set_by="operator",
        )
    )
    s.commit()
    ingestor = EventIngestor(s)
    ingestor.ingest(
        [_make_push("native", account_id="native@example.com", account_source="local")],
        sidecar_id="host-a",
    )
    ingestor.ingest(
        [_make_push("unassigned", account_id="Unassigned", account_source="local")],
        sidecar_id="host-a",
    )
    ingestor.ingest(
        [_make_push("other-host", account_id="default", account_source="default")],
        sidecar_id="host-b",
    )

    rows = {row.event_id: row for row in s.exec(select(UsageEvent)).all()}
    assert rows["native"].account_id == "native@example.com"
    assert rows["native"].attribution_source == "local"
    assert rows["unassigned"].account_id == "Unassigned"
    assert rows["unassigned"].attribution_source == "local"
    assert "other-host" not in rows
    assert s.exec(select(PendingUsageEvent)).one().sidecar_id == "host-b"


def test_unresolved_replay_does_not_requeue_an_already_assigned_event():
    s = _seeded_session()
    assigned = _make_push(event_id="already-assigned", account_id="alice@example.com")
    EventIngestor(s).ingest([assigned], sidecar_id="dev-01")
    unresolved = _make_push(
        event_id="already-assigned", account_id="default", account_source="default"
    )
    result = EventIngestor(s).ingest([unresolved], sidecar_id="dev-01")
    assert result.events_duplicate == 1
    assert s.exec(select(PendingUsageEvent)).all() == []
    assert s.exec(select(UsageEvent)).one().account_id == "alice@example.com"


def test_ingest_persists_claude_code_dimensions_and_prices_cache_split():
    """effort/speed/service_tier/entrypoint/app_version/web-tool counts persist
    verbatim, and the 1h/5m cache split is priced through to cost_cache_create
    (sonnet: 1h @ $6.00/MT, 5m @ $3.75/MT)."""
    s = _seeded_session()
    push = _make_push(
        event_id="msg_dims",
        tokens_cache_create=1_000_000,
        tokens_cache_create_1h=700_000,
        tokens_cache_create_5m=300_000,
        effort="high",
        speed="standard",
        service_tier="standard",
        entrypoint="cli",
        app_version="2.1.217",
        web_search_requests=1,
        web_fetch_requests=2,
    )
    EventIngestor(s).ingest([push], sidecar_id="dev-01")
    ev = s.exec(select(UsageEvent).where(UsageEvent.event_id == "msg_dims")).first()

    assert ev.effort == "high"
    assert ev.speed == "standard"
    assert ev.service_tier == "standard"
    assert ev.entrypoint == "cli"
    assert ev.app_version == "2.1.217"
    assert ev.web_search_requests == 1
    assert ev.web_fetch_requests == 2
    assert ev.tokens_cache_create_1h == 700_000
    assert ev.tokens_cache_create_5m == 300_000
    assert ev.cost_cache_create == round(700_000 / 1_000_000 * 6.00 + 300_000 / 1_000_000 * 3.75, 6)


def test_ingest_then_replay_is_idempotent():
    """Replaying the exact same batch must not double-count anything.

    Property: after ingest(B) then ingest(B) again, the count of usage_events
    rows and the rollup sums are identical to a single ingest(B).
    """
    s = _seeded_session()
    batch = [_make_push(event_id=f"msg_{i}") for i in range(100)]

    # First ingest establishes the baseline.
    EventIngestor(s).ingest(batch, sidecar_id="dev-01")
    baseline_events = len(s.exec(select(UsageEvent)).all())
    baseline_day = s.exec(
        select(UsagePeriodRollup).where(
            UsagePeriodRollup.period_type == "day",
            UsagePeriodRollup.period_key == "2026-05-08",
            UsagePeriodRollup.model_id == "",
            UsagePeriodRollup.sidecar_id == "",
        )
    ).first()
    assert baseline_events == 100
    assert baseline_day.msgs == 100

    # Replay the same batch.
    res = EventIngestor(s).ingest(batch, sidecar_id="dev-01")

    # All 100 should be deduplicated; no new event rows, no rollup drift.
    assert res.events_inserted == 0
    assert res.events_duplicate == 100
    assert len(s.exec(select(UsageEvent)).all()) == baseline_events
    day = s.exec(
        select(UsagePeriodRollup).where(
            UsagePeriodRollup.period_type == "day",
            UsagePeriodRollup.period_key == "2026-05-08",
            UsagePeriodRollup.model_id == "",
            UsagePeriodRollup.sidecar_id == "",
        )
    ).first()
    assert day.msgs == baseline_day.msgs
    assert day.tokens_input == baseline_day.tokens_input
    assert day.tokens_output == baseline_day.tokens_output


def test_partial_batch_failure_rolls_back_all_rollups(monkeypatch):
    """A mid-batch crash must leave neither events nor rollups partially persisted.

    Event-sourcing invariant: rollups are a derived view of usage_events. If
    the ingest transaction fails, neither side may be visible — otherwise
    rollup recomputation from events disagrees with what's stored.
    """
    from app.services import event_ingestor as ingestor_mod

    s = _seeded_session()
    batch = [_make_push(event_id=f"msg_{i}") for i in range(100)]

    real_update = ingestor_mod.update_rollups_for_event
    calls = {"n": 0}

    def boom(session, ev):
        calls["n"] += 1
        if calls["n"] == 50:
            raise RuntimeError("simulated rollup failure on event 50")
        return real_update(session, ev)

    monkeypatch.setattr(ingestor_mod, "update_rollups_for_event", boom)

    try:
        EventIngestor(s).ingest(batch, sidecar_id="dev-01")
    except RuntimeError:
        pass  # expected — injected failure; we verify the rollback below

    # Re-open the session view of the database. The failed ingest must not
    # have left events 1..49 (or their rollups) visible.
    s.rollback()
    events = s.exec(select(UsageEvent)).all()
    rollups = s.exec(select(UsagePeriodRollup)).all()
    assert events == [], f"expected zero events, found {len(events)}"
    assert rollups == [], f"expected zero rollups, found {len(rollups)}"


def _gemini_redirect_session():
    s = _seeded_session()
    s.add(
        CredentialTag(
            provider_id="gemini",
            credential_origin="provider:gemini",
            account_id="me@example.com",
            sidecar_id="host-a",
            set_by="operator",
            target_provider_id="antigravity",
        )
    )
    s.commit()
    return s


def _hermes_gemini_push(event_id: str, **kw) -> UsageEventPush:
    return _make_push(
        event_id=event_id,
        provider_id="gemini",
        account_id="default",
        account_source="default",
        model_id="gemini-3-pro-preview",
        tokens_input=1000,
        tokens_output=500,
        **kw,
    )


def test_hermes_gemini_event_redirected_to_antigravity_is_normalized_and_priced():
    from app.services.data_health.checks.unpriced_models import UnpricedModelsCheck

    s = _gemini_redirect_session()
    EventIngestor(s).ingest(
        [_hermes_gemini_push("hermes|p|s|gemini-3-pro-preview|main|c1s1")], sidecar_id="host-a"
    )

    ev = s.exec(select(UsageEvent)).one()
    assert (ev.provider_id, ev.model_id) == ("antigravity", "pro-3")
    assert ev.cost_usd > 0
    assert UnpricedModelsCheck().detect(s).total_count == 0


def test_non_hermes_gemini_event_keeps_its_model_when_redirected():
    s = _gemini_redirect_session()
    EventIngestor(s).ingest(
        [_hermes_gemini_push("cli-1").model_copy(update={"model_id": "pro-3.1-preview"})],
        sidecar_id="host-a",
    )

    ev = s.exec(select(UsageEvent)).one()
    assert (ev.provider_id, ev.model_id) == ("antigravity", "pro-3.1-preview")


def test_non_redirected_hermes_gemini_event_keeps_raw_model():
    s = _seeded_session()
    push = _hermes_gemini_push("hermes|p|s|gemini-3-pro-preview|main|c1s1").model_copy(
        update={"account_id": "me@example.com", "account_source": "tag"}
    )
    EventIngestor(s).ingest([push], sidecar_id="host-a")
    ev = s.exec(select(UsageEvent)).one()
    assert (ev.provider_id, ev.model_id) == ("gemini", "gemini-3-pro-preview")


def test_directory_event_never_inherits_another_roots_provider_tag():
    s = _seeded_session()
    s.add(
        CredentialTag(
            provider_id="anthropic",
            credential_origin="provider:anthropic",
            account_id="personal@example.com",
            sidecar_id="dev-01",
            set_by="operator",
        )
    )
    s.commit()
    push = _make_push(
        "work-root-event",
        account_id="default",
        account_source="default",
        credential_origin="path:/home/work/projects",
    )
    EventIngestor(s).ingest([push], sidecar_id="dev-01")
    assert s.exec(select(UsageEvent)).first() is None
    pending = s.exec(select(PendingUsageEvent)).one()
    assert "path:/home/work/projects" in pending.payload_json
    s.add(
        CredentialTag(
            provider_id="anthropic",
            credential_origin="path:/home/work/projects",
            account_id="work@example.com",
            sidecar_id="dev-01",
            set_by="operator",
        )
    )
    s.commit()
    EventIngestor(s).ingest([push], sidecar_id="dev-01")
    event = s.exec(select(UsageEvent)).one()
    assert event.account_id == "work@example.com"
    assert event.credential_origin == "path:/home/work/projects"


def test_parked_provider_mapping_does_not_stamp_the_archived_account():
    """A mapping onto an archived account is parked: it resolves the
    credential, but the event must not be billed to the retired account."""
    s = _seeded_session()
    s.add(
        ProviderConfig(
            provider_id="kimi_coding",
            account_id="retired@example.com",
            enabled=False,
            archived=True,
        )
    )
    s.add(
        CredentialTag(
            provider_id="kimi_coding",
            credential_origin="provider:kimi_coding",
            account_id="retired@example.com",
            sidecar_id="host-a",
            set_by="operator",
        )
    )
    s.commit()

    EventIngestor(s).ingest(
        [
            _make_push(
                event_id="parked-event",
                provider_id="kimi_coding",
                account_id="default",
                account_source="default",
            )
        ],
        sidecar_id="host-a",
    )

    # Nothing may be booked on the retired account.
    assert s.exec(select(UsageEvent)).all() == []
    # The event is held for assignment instead of being lost.
    pending = s.exec(select(PendingUsageEvent)).one()
    assert pending.provider_id == "kimi_coding"
    assert pending.event_id == "parked-event"


def test_parked_redirect_does_not_rehome_events_onto_the_archived_account():
    """A redirect tag names an account of its *target* provider, so its
    parked-ness has to be looked up there. A retired target must not receive
    the redirected events."""
    s = _seeded_session()
    s.add(
        ProviderConfig(
            provider_id="antigravity",
            account_id="retired@example.com",
            enabled=False,
            archived=True,
        )
    )
    s.add(
        CredentialTag(
            provider_id="gemini",
            credential_origin="provider:gemini",
            account_id="retired@example.com",
            target_provider_id="antigravity",
            sidecar_id="host-a",
            set_by="operator",
        )
    )
    s.commit()

    EventIngestor(s).ingest(
        [
            _make_push(
                event_id="redirected-event",
                provider_id="gemini",
                account_id="default",
                account_source="default",
            )
        ],
        sidecar_id="host-a",
    )

    events = s.exec(select(UsageEvent)).all()
    assert [event.account_id for event in events] != ["retired@example.com"]
    assert all(event.provider_id != "antigravity" for event in events)


class TestNativeIdentityResolution:
    """An identity-less event is resolved from the host's own native identity.

    A Hermes event names the provider it was billed to, never the account:
    the sidecar only learns that Hermes called MiniMax, not which MiniMax
    account paid. The event therefore arrives with ``account_source="default"``
    and was held for the operator to assign by hand.

    The host's native collector for that provider *does* know — it reported
    the credential, and the server filed it under the account the credential
    resolved to. That row is the machine's own answer (#931).
    """

    @staticmethod
    def _source(s, *, provider_id="minimax", account_id, sidecar_id="host-a", source_id=None):
        s.add(
            CredentialSource(
                provider_id=provider_id,
                account_id=account_id,
                source_id=source_id or f"sidecar:{sidecar_id}:{provider_id}",
                source_type="sidecar",
                source_label="test",
                credential_origin=f"file:~/{provider_id}/auth.json",
                sidecar_id=sidecar_id,
            )
        )
        s.commit()

    @staticmethod
    def _push(event_id="hermes|1", **kw):
        return _make_push(
            event_id=event_id,
            provider_id="minimax",
            account_id="default",
            account_source="default",
            **kw,
        )

    def test_the_hosts_own_identity_resolves_the_event(self):
        s = _seeded_session()
        self._source(s, account_id="me@example.com")
        result = EventIngestor(s).ingest([self._push()], sidecar_id="host-a")
        assert result.events_inserted == 1
        assert s.exec(select(PendingUsageEvent)).all() == []
        row = s.exec(select(UsageEvent)).one()
        assert row.account_id == "me@example.com"
        assert row.attribution_source == "local"

    def test_no_native_identity_holds_the_event_back(self):
        s = _seeded_session()
        EventIngestor(s).ingest([self._push()], sidecar_id="host-a")
        assert s.exec(select(UsageEvent)).all() == []
        assert s.exec(select(PendingUsageEvent)).one().event_id == "hermes|1"

    def test_the_default_sentinel_is_not_an_identity(self):
        """A credential whose identity was never resolved says nothing."""
        s = _seeded_session()
        self._source(s, account_id="default")
        EventIngestor(s).ingest([self._push()], sidecar_id="host-a")
        assert s.exec(select(UsageEvent)).all() == []
        assert len(s.exec(select(PendingUsageEvent)).all()) == 1

    def test_a_pending_identity_slot_is_not_an_account(self):
        """A credential still being verified is filed under a stable slot.

        For Anthropic that slot is the source id — a placeholder, not an
        account. It must never be stamped on.
        """
        s = _seeded_session()
        self._source(
            s,
            provider_id="anthropic",
            account_id="sidecar:host-a:file:~/.claude/.credentials.json",
            source_id="sidecar:host-a:file:~/.claude/.credentials.json",
        )
        push = _make_push(
            event_id="hermes|2",
            provider_id="anthropic",
            account_id="default",
            account_source="default",
        )
        EventIngestor(s).ingest([push], sidecar_id="host-a")
        assert s.exec(select(UsageEvent)).all() == []
        assert len(s.exec(select(PendingUsageEvent)).all()) == 1

    def test_two_accounts_on_the_host_hold_the_event_back(self):
        """Ambiguity is the operator's to resolve, not the server's."""
        s = _seeded_session()
        self._source(s, account_id="one@example.com", source_id="sidecar:host-a:a")
        self._source(s, account_id="two@example.com", source_id="sidecar:host-a:b")
        EventIngestor(s).ingest([self._push()], sidecar_id="host-a")
        assert s.exec(select(UsageEvent)).all() == []
        assert len(s.exec(select(PendingUsageEvent)).all()) == 1

    def test_another_hosts_identity_does_not_reach_this_one(self):
        """The identity is scoped to the host that reported it (#319)."""
        s = _seeded_session()
        self._source(s, account_id="other@example.com", sidecar_id="host-b")
        EventIngestor(s).ingest([self._push()], sidecar_id="host-a")
        assert s.exec(select(UsageEvent)).all() == []
        assert len(s.exec(select(PendingUsageEvent)).all()) == 1

    def test_an_operator_tag_wins_over_the_native_identity(self):
        s = _seeded_session()
        self._source(s, account_id="native@example.com")
        s.add(
            CredentialTag(
                provider_id="minimax",
                credential_origin="provider:minimax",
                account_id="tagged@example.com",
                sidecar_id="host-a",
                set_by="operator",
            )
        )
        s.commit()
        EventIngestor(s).ingest([self._push()], sidecar_id="host-a")
        row = s.exec(select(UsageEvent)).one()
        assert row.account_id == "tagged@example.com"
        assert row.attribution_source == "tag"

    def test_an_archived_account_holds_the_event_back(self):
        """A retired account takes no new usage, resolved or not."""
        s = _seeded_session()
        self._source(s, account_id="gone@example.com")
        s.add(
            ProviderConfig(
                provider_id="minimax",
                account_id="gone@example.com",
                archived=True,
            )
        )
        s.commit()
        EventIngestor(s).ingest([self._push()], sidecar_id="host-a")
        assert s.exec(select(UsageEvent)).all() == []
        assert len(s.exec(select(PendingUsageEvent)).all()) == 1

    def test_an_event_that_resolves_later_leaves_no_pending_copy(self):
        """A held-back event can resolve on a later push.

        Its pending copy is then stale: the "Unassigned usage" card would
        keep listing an event that is already counted.
        """
        s = _seeded_session()
        push = self._push(event_id="hermes|late")
        EventIngestor(s).ingest([push], sidecar_id="host-a")
        assert len(s.exec(select(PendingUsageEvent)).all()) == 1

        # The host's native identity now answers for the provider.
        self._source(s, account_id="me@example.com")
        EventIngestor(s).ingest([push], sidecar_id="host-a")

        assert s.exec(select(PendingUsageEvent)).all() == []
        assert s.exec(select(UsageEvent)).one().account_id == "me@example.com"
