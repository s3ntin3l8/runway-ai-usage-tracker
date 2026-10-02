import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.models.db import ProviderConfig, SystemConfig, WebhookConfig, WebhookCredentialAlert
from app.services import auth_failures


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


@pytest.fixture(name="engine")
def engine_fixture():
    """A shared StaticPool in-memory engine two independent Sessions can both
    bind to — used by the overlapping-poll race test below."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _row(
    provider="anthropic",
    account_id="alice@example.com",
    status="valid",
    account_label=None,
    token_types=None,
    redundant=False,
    source_name="config",
):
    """A Token Health record shaped like `TokenHealthService.get_health()` output."""
    return {
        "provider": provider,
        "account_id": account_id,
        "account_label": account_label,
        "source": "config",
        "source_name": source_name,
        "token_types": token_types or [],
        "status": status,
        "expires_at": None,
        "ttl_remaining_seconds": 0,
        "can_refresh": False,
        "removable": True,
        "redundant": redundant,
    }


def _config(
    session,
    provider="anthropic",
    account=None,
    channel="discord",
    url="https://discord.example.com/webhook",
    credential_alerts=True,
    active=True,
):
    cfg = WebhookConfig(
        provider_id=provider,
        account_id=account,
        threshold_pct=90.0,
        url=url,
        channel=channel,
        active=active,
        credential_alerts=credential_alerts,
    )
    session.add(cfg)
    session.commit()
    session.refresh(cfg)
    return cfg


def _mock_client(post=None):
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    if post is not None:
        mock_client.post = post
    else:
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)
    return mock_client


async def _run(session, rows, client=None):
    from app.services.credential_alerts import check_credential_alerts

    client = client or _mock_client()
    with (
        patch(
            "app.services.credential_alerts.token_health_service.get_health",
            new=AsyncMock(return_value=rows),
        ),
        patch("app.services.credential_alerts.httpx.AsyncClient") as mock_cls,
    ):
        mock_cls.return_value = client
        await check_credential_alerts(session)
    return client


@pytest.mark.asyncio
async def test_fires_on_invalid(session):
    _config(session)
    client = await _run(session, [_row(status="invalid")])
    assert client.post.called
    alert = session.exec(select(WebhookCredentialAlert)).one()
    assert alert.status == "invalid"


@pytest.mark.asyncio
async def test_fires_on_a_credential_that_keeps_failing_to_collect(session):
    _config(session)
    client = await _run(session, [_row(status="failing")])
    assert client.post.called
    assert session.exec(select(WebhookCredentialAlert)).one().status == "failing"
    assert "failing to collect" in str(client.post.call_args)


@pytest.mark.asyncio
async def test_failing_alert_escalates_to_invalid_but_never_back_down(session):
    _config(session)
    await _run(session, [_row(status="failing")])
    await _run(session, [_row(status="invalid")])
    assert session.exec(select(WebhookCredentialAlert)).one().status == "invalid"
    await _run(session, [_row(status="failing")])
    assert session.exec(select(WebhookCredentialAlert)).one().status == "invalid"


@pytest.mark.asyncio
async def test_a_working_sibling_suppresses_a_failing_alert(session):
    _config(session)
    rows = [
        _row(account_id="alice@example.com", status="failing"),
        _row(account_id="alice@example.com", status="valid", source_name="cache"),
    ]
    assert not (await _run(session, rows)).post.called


@pytest.mark.asyncio
async def test_fires_on_non_rollable_expired(session):
    _config(session)
    client = await _run(session, [_row(status="expired", token_types=["access_token"])])
    assert client.post.called


@pytest.mark.asyncio
async def test_pending_expired_identity_is_visible_but_not_account_alerted(session):
    _config(session)
    pending = {**_row(status="expired"), "identity_pending": True}

    client = await _run(session, [pending])

    assert not client.post.called
    assert session.exec(select(WebhookCredentialAlert)).all() == []


@pytest.mark.asyncio
async def test_rollable_expired_does_not_fire(session):
    """A refresh_token-bearing expired row is a normal OAuth rollover, not a rejection."""
    _config(session)
    client = await _run(
        session, [_row(status="expired", token_types=["access_token", "refresh_token"])]
    )
    assert not client.post.called


@pytest.mark.asyncio
async def test_xai_refresh_expired_does_not_fire(session):
    """xAI keeps its refresh token as ``xai_refresh``; the server can roll it like any other."""
    _config(session)
    client = await _run(
        session, [_row(status="expired", token_types=["xai_access", "xai_refresh"])]
    )
    assert not client.post.called


@pytest.mark.asyncio
async def test_flagged_rollable_expired_row_still_fires(session):
    """A revoked refresh_token: TokenAutoRefresher only logs the failure (never
    flags auth_failures), and Token Health's own _apply_invalid skips rows
    already `expired` — so this row is stuck at status="expired" with
    token_types including refresh_token forever. A live collection attempt
    failing with 401/403 still flags auth_failures directly, and that must be
    enough to alert even though the row's own status never becomes invalid.
    """
    _config(session)
    auth_failures.mark("anthropic", "alice@example.com")
    client = await _run(
        session,
        [
            _row(
                account_id="alice@example.com",
                status="expired",
                token_types=["access_token", "refresh_token"],
            )
        ],
    )
    assert client.post.called


@pytest.mark.asyncio
async def test_redundant_expired_does_not_fire(session):
    _config(session)
    client = await _run(session, [_row(status="expired", redundant=True)])
    assert not client.post.called


@pytest.mark.asyncio
async def test_healthy_sibling_suppresses_alert(session):
    """A working credential alongside a stale one must not page."""
    _config(session)
    rows = [
        _row(account_id="alice@example.com", status="expired"),
        _row(account_id="alice@example.com", status="valid", source_name="cache"),
    ]
    client = await _run(session, rows)
    assert not client.post.called


@pytest.mark.asyncio
async def test_stale_sibling_does_not_suppress_a_real_alert(session):
    """A ``stale`` row (a machine that stopped reporting) is no evidence the account
    still works, so it must not hold off the alert for a genuinely expired credential
    — which a stored-as-``valid`` row for a removed machine used to do forever."""
    _config(session)
    rows = [
        _row(account_id="alice@example.com", status="expired"),
        _row(account_id="alice@example.com", status="stale", source_name="gone-host"),
    ]
    client = await _run(session, rows)
    assert client.post.called


@pytest.mark.asyncio
async def test_stale_row_alone_never_alerts(session):
    """...and a stale row is not itself alert-worthy (no repeat pages for a removed machine)."""
    _config(session)
    client = await _run(session, [_row(status="stale", source_name="gone-host")])
    assert not client.post.called


@pytest.mark.asyncio
async def test_no_refire_on_next_run_including_escalation(session):
    _config(session)
    client1 = await _run(session, [_row(status="expired")])
    assert client1.post.called

    client2 = await _run(session, [_row(status="invalid")])
    assert not client2.post.called
    # the dedup row escalates its recorded status for informational value
    alert = session.exec(select(WebhookCredentialAlert)).one()
    assert alert.status == "invalid"


@pytest.mark.asyncio
async def test_missing_key_keeps_state(session):
    """An account absent from the current Token Health snapshot (e.g. a cache
    TTL gap right after restart) must not re-arm an active alert."""
    _config(session)
    await _run(session, [_row(status="invalid")])

    client = await _run(session, [_row(account_id="someone-else@example.com", status="valid")])
    assert not client.post.called
    alerts = session.exec(select(WebhookCredentialAlert)).all()
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_no_immediate_rearm_on_single_healthy_tick(session):
    """Flapping invalid -> valid -> invalid within the re-arm window sends one alert."""
    _config(session)
    client1 = await _run(session, [_row(status="invalid")])
    assert client1.post.call_count == 1

    client2 = await _run(session, [_row(status="valid")])
    assert not client2.post.called
    alert = session.exec(select(WebhookCredentialAlert)).one()
    assert alert.healthy_since is not None

    client3 = await _run(session, [_row(status="invalid")])
    assert not client3.post.called  # still the same episode
    alert = session.exec(select(WebhookCredentialAlert)).one()
    assert alert.healthy_since is None  # cleared by the renewed bad observation


@pytest.mark.asyncio
async def test_rearms_after_window_then_fires_again(session):
    """The first healthy tick only starts the clock (`healthy_since`); the row
    is deleted — re-arming — on a later tick once the window has elapsed."""
    _config(session)
    await _run(session, [_row(status="invalid")])

    with patch("app.services.credential_alerts._rearm_window_seconds", return_value=0):
        client = await _run(session, [_row(status="valid")])
        assert not client.post.called
        alerts = session.exec(select(WebhookCredentialAlert)).all()
        assert len(alerts) == 1

        client = await _run(session, [_row(status="valid")])
        assert not client.post.called
        alerts = session.exec(select(WebhookCredentialAlert)).all()
        assert alerts == []

    client = await _run(session, [_row(status="invalid")])
    assert client.post.called


def test_rearm_window_scales_with_configured_poll_interval(session):
    """The 1800s floor only holds at the 900s poller default — a longer
    configured interval must still require at least two of *its* cycles,
    or a single healthy observation could re-arm after a much longer gap
    than intended."""
    from app.services.credential_alerts import _rearm_window_seconds

    assert _rearm_window_seconds(session) == 1800  # no SystemConfig row: default floor

    session.add(SystemConfig(default_poll_interval_seconds=1200))
    session.commit()
    assert _rearm_window_seconds(session) == 2400  # 2x the configured interval


@pytest.mark.asyncio
async def test_synthetic_config_prefix_matches_scoped_webhook(session):
    _config(session, account="alice@example.com")
    client = await _run(
        session, [_row(account_id="config:alice@example.com", status="invalid", account_label=None)]
    )
    assert client.post.called


@pytest.mark.asyncio
async def test_server_credential_maps_to_default_account(session):
    _config(session, account="default")
    client = await _run(session, [_row(account_id="server", status="invalid", account_label=None)])
    assert client.post.called


@pytest.mark.asyncio
async def test_server_row_falls_back_to_provider_config_label_for_scope_matching(session):
    """A `server` (env/file-discovered) row carries no account_label of its
    own. A webhook scoped to the resolved email must still match it when a
    provider_configs row (used only for labeling here) says the `default`
    account resolves to that email — the same as it already would for cards.
    """
    session.add(
        ProviderConfig(
            provider_id="anthropic", account_id="default", account_label="work@example.com"
        )
    )
    session.commit()
    _config(session, account="work@example.com")

    client = await _run(session, [_row(account_id="server", status="invalid", account_label=None)])
    assert client.post.called


@pytest.mark.asyncio
async def test_wildcard_config_matches_any_provider(session):
    _config(session, provider="*")
    client = await _run(session, [_row(provider="openai", status="invalid")])
    assert client.post.called


@pytest.mark.asyncio
async def test_provider_mismatch_does_not_fire(session):
    _config(session, provider="anthropic")
    client = await _run(session, [_row(provider="openai", status="invalid")])
    assert not client.post.called


@pytest.mark.asyncio
async def test_credential_alerts_toggle_off_suppresses_fire(session):
    _config(session, credential_alerts=False)
    client = await _run(session, [_row(status="invalid")])
    assert not client.post.called


@pytest.mark.asyncio
async def test_inactive_webhook_does_not_fire(session):
    _config(session, active=False)
    client = await _run(session, [_row(status="invalid")])
    assert not client.post.called


@pytest.mark.asyncio
async def test_failed_delivery_retries_next_run(session):
    _config(session)
    failing_client = _mock_client(post=AsyncMock(side_effect=Exception("boom")))
    await _run(session, [_row(status="invalid")], client=failing_client)
    alerts = session.exec(select(WebhookCredentialAlert)).all()
    assert alerts == []

    client2 = await _run(session, [_row(status="invalid")])
    assert client2.post.called
    alerts = session.exec(select(WebhookCredentialAlert)).all()
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_two_webhooks_deduped_independently(session):
    _config(session, url="https://discord.example.com/a")
    _config(session, channel="slack", url="https://hooks.slack.com/b")

    client = await _run(session, [_row(status="invalid")])
    assert client.post.call_count == 2

    client2 = await _run(session, [_row(status="invalid")])
    assert client2.post.call_count == 0


@pytest.mark.asyncio
async def test_overlapping_polls_do_not_double_deliver(engine):
    """`poll_now()` isn't reentrancy-guarded — a scheduled tick and a
    `POST /force-collect` can both call `check_credential_alerts` concurrently
    on the same event loop. Without `_check_lock`, both cycles can read the
    dedup row as absent before either writes it, and both deliver the
    webhook. Two independent Sessions on a shared engine + `asyncio.gather`
    reproduces the real interleaving; mocking `_post_payload` to actually
    `await asyncio.sleep(0)` opens the same race window a real HTTP POST
    would.
    """
    from app.services.credential_alerts import check_credential_alerts

    with Session(engine) as session_a, Session(engine) as session_b:
        _config(session_a)

        rows = [_row(status="invalid")]

        async def _slow_post(*_args, **_kwargs) -> None:
            await asyncio.sleep(0)

        post_mock = AsyncMock(side_effect=_slow_post)

        with (
            patch(
                "app.services.credential_alerts.token_health_service.get_health",
                new=AsyncMock(return_value=rows),
            ),
            patch("app.services.credential_alerts._post_payload", new=post_mock),
        ):
            await asyncio.gather(
                check_credential_alerts(session_a),
                check_credential_alerts(session_b),
            )

        assert post_mock.call_count == 1
        alerts = session_a.exec(select(WebhookCredentialAlert)).all()
        assert len(alerts) == 1
        # Pin the StaticPool-shared-connection assumption this test relies
        # on: session_b must see the same committed row session_a does, not
        # a second, independent one from an unserialized write.
        alerts_from_b = session_b.exec(select(WebhookCredentialAlert)).all()
        assert len(alerts_from_b) == 1


def test_commit_step_recovers_from_a_dedup_race(session):
    """`_check_lock` now serializes overlapping `check_credential_alerts`
    cycles, so this scenario shouldn't arise in production — but `_commit_step`
    keeps its `IntegrityError` catch as a defense-in-depth backstop (e.g. a
    future caller that bypasses the lock, or a stray duplicate row from
    before this fix shipped). It must still swallow a losing insert's
    IntegrityError and leave the session usable for the rest of that cycle's
    rows, rather than rolling back everything committed so far."""
    from app.services.credential_alerts import _commit_step

    session.add(
        WebhookCredentialAlert(
            webhook_id=1, provider_id="anthropic", account_id="default", status="invalid"
        )
    )
    session.commit()

    # Simulate the race: this cycle didn't see the row another cycle already
    # committed, and tries to insert the same (webhook_id, provider, account).
    session.add(
        WebhookCredentialAlert(
            webhook_id=1, provider_id="anthropic", account_id="default", status="invalid"
        )
    )
    _commit_step(session, "test race")  # must not raise

    # The session recovers: unrelated rows still commit fine afterward.
    session.add(
        WebhookCredentialAlert(
            webhook_id=2, provider_id="anthropic", account_id="default", status="invalid"
        )
    )
    session.commit()
    alerts = session.exec(select(WebhookCredentialAlert)).all()
    assert len(alerts) == 2


def test_credential_discord_payload_shape():
    from app.services.webhooks import _credential_discord_payload

    payload = _credential_discord_payload(
        "anthropic", "alice@example.com", "alice@example.com", "invalid", "sidecar1"
    )
    embed = payload["embeds"][0]
    assert embed["title"] == "Credential invalid (rejected by provider)"
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields == {
        "Provider": "anthropic",
        "Account": "alice@example.com",
        "Status": "invalid",
        "Source": "sidecar1",
    }


def test_credential_slack_payload_shape():
    from app.services.webhooks import _credential_slack_payload

    payload = _credential_slack_payload("anthropic", None, "acct1", "expired", None)
    header = payload["blocks"][0]
    assert header["text"]["text"] == "Credential expired"
    context_text = " ".join(e["text"] for e in payload["blocks"][1]["elements"])
    assert "*Account:* acct1" in context_text
    assert "*Source:* unknown" in context_text


@pytest.mark.asyncio
async def test_no_configs_skips_token_health_scan(session):
    """No active/opted-in webhooks: bail before ever calling get_health()."""
    from app.services.credential_alerts import check_credential_alerts

    with patch(
        "app.services.credential_alerts.token_health_service.get_health", new=AsyncMock()
    ) as mock_get_health:
        await check_credential_alerts(session)

    assert not mock_get_health.called


@pytest.mark.asyncio
async def test_healthy_key_with_no_prior_alert_is_a_noop(session):
    _config(session)
    client = await _run(session, [_row(status="valid")])
    assert not client.post.called
    alerts = session.exec(select(WebhookCredentialAlert)).all()
    assert alerts == []
