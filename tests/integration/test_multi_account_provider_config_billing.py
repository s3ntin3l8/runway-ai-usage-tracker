"""Integration tests for billing model detection, subscription preview, and pricing choices."""

from __future__ import annotations

from tests.integration._multi_account_provider_config_helpers import (
    CONSOLE_ORG_ID,
    _admin_headers,
    _mock_console_access,
    client_fixture,  # noqa: F401 — fixture
    session_fixture,  # noqa: F401 — fixture
)

CLAUDE_ORG_ID = "22222222-2222-4222-8222-222222222222"


def _mock_claude_api(
    monkeypatch, calls: list[str], organization_ids: tuple[str, ...] = (CLAUDE_ORG_ID,)
) -> None:
    """Serve the claude.ai endpoints the verification reads."""
    import httpx

    original_client = httpx.AsyncClient

    def upstream(request):
        calls.append(str(request.url))
        if request.url.path == "/api/account":
            return httpx.Response(200, json={"email_address": "person@example.com"})
        if request.url.path == "/api/organizations":
            return httpx.Response(
                200,
                json=[{"uuid": oid, "name": f"Org {oid[:8]}"} for oid in organization_ids],
            )
        return httpx.Response(200, json={"five_hour": {"utilization": 10}})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(upstream), **kwargs),
    )


def _mock_oauth_usage(monkeypatch, calls: list[str], payload: dict) -> None:
    """Serve the OAuth usage endpoint the manual pricing path reads."""
    import httpx

    original_client = httpx.AsyncClient

    def upstream(request):
        calls.append(str(request.url))
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(upstream), **kwargs),
    )


def test_the_console_preview_suggests_the_billing_model(client, session, monkeypatch):
    """Console publishes prepaid credit, so the account spends — proved, not assumed."""
    _mock_console_access(monkeypatch)
    preview = client.post(
        "/api/v1/system/provider-config/preview-account",
        json={
            "provider_id": "anthropic_console",
            "session_cookie": "sample-session",
            "organization_id": CONSOLE_ORG_ID,
        },
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["suggested_billing_type"] == "pay_as_you_go"


def test_the_subscription_preview_detects_the_billing_model_when_asked(client, monkeypatch):
    """The operator asks for the model; the account's own quota windows decide."""
    calls: list[str] = []
    _mock_claude_api(monkeypatch, calls)
    preview = client.post(
        "/api/v1/system/provider-config/preview-account",
        json={
            "provider_id": "anthropic",
            "session_cookie": "sample-session",
            "detect_billing": True,
        },
    )
    assert preview.status_code == 200, preview.text
    # A claude.ai plan publishes percentage-of-limit windows — a subscription.
    assert preview.json()["suggested_billing_type"] == "subscription"
    assert calls, "the detection must actually ask Claude"


def test_the_subscription_preview_does_not_call_claude_unless_asked(client, monkeypatch):
    """The wizard debounces keystrokes into the preview; a proof costs a round trip."""
    calls: list[str] = []
    _mock_claude_api(monkeypatch, calls)
    preview = client.post(
        "/api/v1/system/provider-config/preview-account",
        json={"provider_id": "anthropic", "session_cookie": "sample-session"},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["suggested_billing_type"] is None
    assert calls == []


def test_billing_detection_is_refused_for_a_provider_that_has_no_claude_account(
    client, monkeypatch
):
    calls: list[str] = []
    _mock_claude_api(monkeypatch, calls)
    preview = client.post(
        "/api/v1/system/provider-config/preview-account",
        json={"provider_id": "chatgpt", "session_cookie": "sample-session", "detect_billing": True},
    )
    assert preview.status_code == 422
    assert calls == []


def test_several_organizations_that_agree_still_prove_the_model(client, monkeypatch):
    """Which organization the account is bound to is the operator's choice.

    How it *bills* need not wait for that choice: every accessible
    organization describing the same way of billing is what proves it.
    """
    calls: list[str] = []
    _mock_claude_api(
        monkeypatch,
        calls,
        organization_ids=(CLAUDE_ORG_ID, "33333333-3333-4333-8333-333333333333"),
    )
    preview = client.post(
        "/api/v1/system/provider-config/preview-account",
        json={
            "provider_id": "anthropic",
            "session_cookie": "sample-session",
            "detect_billing": True,
        },
    )
    assert preview.status_code == 200, preview.text
    # Both organizations publish percentage-of-limit windows: a subscription.
    assert preview.json()["suggested_billing_type"] == "subscription"


def test_an_inconclusive_detection_is_not_a_decision(client, monkeypatch):
    """The absence of a proof is not a proof of "unknown".

    One organization answers with something that says nothing about how it
    bills: the detection is incomplete, so the model stays undecided. Storing
    "unknown" would pin the account to it — a decision nobody made.
    """
    import httpx

    calls: list[str] = []
    original_client = httpx.AsyncClient

    def upstream(request):
        calls.append(str(request.url))
        if request.url.path == "/api/account":
            return httpx.Response(200, json={"email_address": "person@example.com"})
        if request.url.path == "/api/organizations":
            return httpx.Response(
                200,
                json=[
                    {"uuid": CLAUDE_ORG_ID, "name": "A"},
                    {"uuid": "33333333-3333-4333-8333-333333333333", "name": "B"},
                ],
            )
        if "33333333" in request.url.path:
            # An organization that publishes nothing about its billing model.
            return httpx.Response(200, json={"five_hour": {}})
        return httpx.Response(200, json={"five_hour": {"utilization": 10}})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(upstream), **kwargs),
    )
    preview = client.post(
        "/api/v1/system/provider-config/preview-account",
        json={
            "provider_id": "anthropic",
            "session_cookie": "sample-session",
            "detect_billing": True,
        },
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["suggested_billing_type"] is None


def test_an_incomplete_detection_is_not_a_decision(client, monkeypatch):
    """A partial read proves nothing, however much the rest agrees.

    Two organizations answer and describe the same way of billing; a third
    answers with something unusable. The read is therefore partial, and the
    third may well bill differently — so the two that answered must not decide
    the model on their own.
    """
    import httpx

    calls: list[str] = []
    original_client = httpx.AsyncClient
    unusable_org = "44444444-4444-4444-8444-444444444444"

    def upstream(request):
        calls.append(str(request.url))
        if request.url.path == "/api/account":
            return httpx.Response(200, json={"email_address": "person@example.com"})
        if request.url.path == "/api/organizations":
            return httpx.Response(
                200,
                json=[
                    {"uuid": CLAUDE_ORG_ID, "name": "A"},
                    {"uuid": "33333333-3333-4333-8333-333333333333", "name": "B"},
                    {"uuid": unusable_org, "name": "C"},
                ],
            )
        if unusable_org in request.url.path:
            # An organization that publishes nothing about its billing model.
            return httpx.Response(200, json={"five_hour": {}})
        return httpx.Response(200, json={"five_hour": {"utilization": 10}})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(upstream), **kwargs),
    )
    preview = client.post(
        "/api/v1/system/provider-config/preview-account",
        json={
            "provider_id": "anthropic",
            "session_cookie": "sample-session",
            "detect_billing": True,
        },
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["suggested_billing_type"] is None


def test_the_oauth_token_preview_detects_the_billing_model_when_asked(client, monkeypatch):
    """An OAuth token has no organization to read; its own usage decides."""
    calls: list[str] = []
    _mock_oauth_usage(monkeypatch, calls, {"five_hour": {"utilization": 25.0}})
    preview = client.post(
        "/api/v1/system/provider-config/preview-account",
        json={
            "provider_id": "anthropic",
            "api_key": "sk-ant-oat-sample",  # pragma: allowlist secret
            "detect_billing": True,
        },
    )
    assert preview.status_code == 200, preview.text
    # A claude.ai plan publishes percentage-of-limit windows: a subscription.
    assert preview.json()["suggested_billing_type"] == "subscription"
    assert calls, "the detection must actually ask Anthropic"


def test_billing_detection_needs_a_credential(client):
    """There is nothing to detect from an empty credential."""
    preview = client.post(
        "/api/v1/system/provider-config/preview-account",
        json={"provider_id": "anthropic", "detect_billing": True},
    )
    assert preview.status_code == 422
    assert "OAuth token" in preview.json()["detail"]


def test_a_conflict_still_carries_the_proof(client, monkeypatch, session):
    """The wizard shows the model even when the account already exists."""
    from app.models.db import ProviderConfig

    calls: list[str] = []
    _mock_oauth_usage(monkeypatch, calls, {"five_hour": {"utilization": 25.0}})
    api_key = "sk-ant-oat-sample"  # pragma: allowlist secret
    # The credential's own account id: the preview is what derives it.
    account_id = client.post(
        "/api/v1/system/provider-config/preview-account",
        json={"provider_id": "anthropic", "api_key": api_key},
    ).json()["suggested_account_id"]
    session.add(
        ProviderConfig(
            provider_id="anthropic",
            account_id=account_id,
        )
    )
    session.commit()

    preview = client.post(
        "/api/v1/system/provider-config/preview-account",
        json={
            "provider_id": "anthropic",
            "api_key": "sk-ant-oat-sample",  # pragma: allowlist secret
            "detect_billing": True,
        },
    )
    assert preview.status_code == 409, preview.text
    assert preview.json()["detail"]["suggested_billing_type"] == "subscription"


def test_the_operator_pricing_an_account_is_recorded_as_their_choice(client, session):
    """A model the operator picked is theirs, and the repair leaves it alone."""
    from sqlmodel import select

    from app.models.db import ProviderConfig

    saved = client.put(
        "/api/v1/system/provider-config/anthropic/person@example.com",
        json={"billing_type": "pay_as_you_go"},
        headers=_admin_headers(),
    )
    assert saved.status_code == 200, saved.text

    row = session.exec(
        select(ProviderConfig).where(
            ProviderConfig.provider_id == "anthropic",
            ProviderConfig.account_id == "person@example.com",
        )
    ).one()
    assert row.billing_type == "pay_as_you_go"
    assert row.billing_type_source == "operator"


def test_the_operator_choosing_unknown_is_still_their_choice(client, session):
    """A deliberate "unknown" is a decision, so the repair leaves it alone."""
    from sqlmodel import select

    from app.models.db import ProviderConfig

    saved = client.put(
        "/api/v1/system/provider-config/anthropic/person@example.com",
        json={"billing_type": "unknown"},
        headers=_admin_headers(),
    )
    assert saved.status_code == 200, saved.text

    row = session.exec(
        select(ProviderConfig).where(
            ProviderConfig.provider_id == "anthropic",
            ProviderConfig.account_id == "person@example.com",
        )
    ).one()
    assert row.billing_type == "unknown"
    assert row.billing_type_source == "operator"


def test_saving_an_account_without_a_model_leaves_the_source_alone(client, session):
    """Saving an account that names no model decides nothing."""
    from sqlmodel import select

    from app.models.db import ProviderConfig

    saved = client.put(
        "/api/v1/system/provider-config/anthropic/person@example.com",
        json={"account_label": "Person"},
        headers=_admin_headers(),
    )
    assert saved.status_code == 200, saved.text

    row = session.exec(
        select(ProviderConfig).where(
            ProviderConfig.provider_id == "anthropic",
            ProviderConfig.account_id == "person@example.com",
        )
    ).one()
    assert row.billing_type == "unknown"
    assert row.billing_type_source is None
