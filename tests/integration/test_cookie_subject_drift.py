"""A browser switching users behind a Kimi / opencode cookie tag is caught (#472).

Those collectors never learn an email, but their login carries a stable subject (the Kimi
cookie's JWT ``sub``, opencode's ``subscriberUserId``). The subject recorded for a tagged
source is compared on every poll: a different one for the same account drops the tag and puts
the source back to pending, exactly like an email switch (#462).
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest
from sqlmodel import Session, select

from app.models.db import CredentialSource
from app.services.credential_tags import CredentialTagRepo
from tests.integration.test_cookie_account_switch import (
    SIDECAR,
    _poll,
    _tagged_source,
    world,  # noqa: F401 — fixture
)

ACCOUNT = "kimi-seat-1"
KIMI_ORIGIN = "cookie:kimi_coding/kimi-auth"
KIMI_SOURCE = f"sidecar:{SIDECAR}:{KIMI_ORIGIN}"
OC_ORIGIN = "cookie:opencode/auth"
USAGES = {
    "usages": [
        {
            "scope": "FEATURE_CODING",
            "limits": [
                {
                    "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                    "detail": {
                        "limit": "100",
                        "used": "5",
                        "remaining": "95",
                        "resetTime": "2099-01-01T00:00:00Z",
                    },
                }
            ],
        }
    ]
}


def _jwt(sub: str | None) -> str:
    def b64(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{b64({'alg': 'none'})}.{b64({'sub': sub} if sub else {})}.sig"


def _kimi(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/GetUsages"):
        return httpx.Response(200, json=USAGES)
    return httpx.Response(404)


async def _kimi_source(world, subject: str | None, account: str = ACCOUNT):  # noqa: F811
    await _tagged_source(
        world,
        account,
        provider="kimi_coding",
        origin=KIMI_ORIGIN,
        tokens={"cookie_kimi-auth": _jwt(subject)},
    )


async def _swap_cookie(world, subject: str | None, account: str = ACCOUNT):  # noqa: F811
    """The sidecar pushes the browser's new cookie for the same origin."""
    _, cache = world
    await cache.store(
        "kimi_coding",
        {"cookie_kimi-auth": _jwt(subject)},
        account_id=account,
        source_id=KIMI_SOURCE,
        source=SIDECAR,
        source_metadata={
            "source_type": "cookie",
            "sidecar_id": SIDECAR,
            "credential_origin": KIMI_ORIGIN,
            "identity_pending": False,
        },
    )


def _kimi_state(engine):
    with Session(engine) as session:
        row = session.exec(
            select(CredentialSource).where(CredentialSource.provider_id == "kimi_coding")
        ).one()
        tag = CredentialTagRepo.get(
            session,
            provider_id="kimi_coding",
            credential_origin=KIMI_ORIGIN,
            sidecar_id=SIDECAR,
        )
        session.expunge(row)
        return row, tag


@pytest.mark.asyncio
async def test_a_kimi_cookie_for_a_different_user_drops_the_tag_and_publishes_nothing(world):  # noqa: F811
    engine, cache = world
    await _kimi_source(world, "user-1")

    assert await _poll(ACCOUNT, _kimi, "kimi_coding"), "the first poll records the subject"
    row, tag = _kimi_state(engine)
    assert tag is not None and row.verified_subject == "user-1"

    await _swap_cookie(world, "user-2")
    result = await _poll(ACCOUNT, _kimi, "kimi_coding")

    assert result == [], "the new user's usage must not be filed under the old account"
    row, tag = _kimi_state(engine)
    assert tag is None
    assert row.account_id == "default", (
        "pending again (only Anthropic keys pending rows by source id)"
    )
    assert row.verified_subject is None
    assert await cache.get_source_candidates("kimi_coding", ACCOUNT) == []


@pytest.mark.asyncio
async def test_a_refreshed_cookie_for_the_same_user_keeps_the_tag(world):  # noqa: F811
    engine, _ = world
    await _kimi_source(world, "user-1")
    await _poll(ACCOUNT, _kimi, "kimi_coding")

    await _swap_cookie(world, "user-1")  # a different token string, the same login
    result = await _poll(ACCOUNT, _kimi, "kimi_coding")

    assert result
    assert _kimi_state(engine)[1] is not None


@pytest.mark.asyncio
async def test_a_cookie_without_a_subject_never_drops_the_tag(world):  # noqa: F811
    engine, _ = world
    await _kimi_source(world, "user-1")
    await _poll(ACCOUNT, _kimi, "kimi_coding")

    await _swap_cookie(world, None)
    await _poll(ACCOUNT, _kimi, "kimi_coding")

    row, tag = _kimi_state(engine)
    assert tag is not None and row.verified_subject == "user-1"


@pytest.mark.asyncio
async def test_a_retag_to_another_account_records_the_subject_instead_of_comparing(world):  # noqa: F811
    """The operator moved the source to another account: whoever is logged in now is the
    baseline for it, not a drift from the old account's user."""
    engine, _ = world
    await _kimi_source(world, "user-1")
    await _poll(ACCOUNT, _kimi, "kimi_coding")
    with Session(engine) as session:
        row = session.exec(select(CredentialSource)).one()
        row.account_id = "kimi-seat-2"
        session.add(row)
        CredentialTagRepo.set_tag(
            session,
            provider_id="kimi_coding",
            credential_origin=KIMI_ORIGIN,
            account_id="kimi-seat-2",
            sidecar_id=SIDECAR,
            set_by="operator",
        )
        session.commit()
    await _swap_cookie(world, "user-2", account="kimi-seat-2")

    result = await _poll("kimi-seat-2", _kimi, "kimi_coding")

    assert result
    row, tag = _kimi_state(engine)
    assert tag is not None and row.verified_subject == "user-2"
    assert row.verified_subject_account == "kimi-seat-2"


@pytest.mark.asyncio
async def test_an_all_machines_tag_is_left_for_the_operator(world):  # noqa: F811
    engine, _ = world
    await _tagged_source(
        world,
        ACCOUNT,
        provider="kimi_coding",
        origin=KIMI_ORIGIN,
        tokens={"cookie_kimi-auth": _jwt("user-1")},
        scope=None,
    )
    await _poll(ACCOUNT, _kimi, "kimi_coding")
    await _swap_cookie(world, "user-2")

    result = await _poll(ACCOUNT, _kimi, "kimi_coding")

    with Session(engine) as session:
        assert (
            CredentialTagRepo.get(
                session,
                provider_id="kimi_coding",
                credential_origin=KIMI_ORIGIN,
                sidecar_id=None,
            )
            is not None
        )
    # Nothing is revoked, so the source keeps publishing and keeps its baseline.
    assert result
    assert _kimi_state(engine)[0].verified_subject == "user-1"


def test_a_subject_seen_for_one_source_attempt_is_never_read_as_the_next_ones():
    from app.services.collector_manager import CollectorManager
    from app.services.collectors.kimi_coding import KimiCodingCollector

    collector = KimiCodingCollector(account_id="default")
    collector.verified_subject = "user-of-source-a"
    collector.verified_identity = "a@example.com"

    CollectorManager._reset_attempt_identity(collector, "default", None)

    assert collector.verified_subject is None and collector.verified_identity is None


@pytest.mark.asyncio
async def test_a_subject_is_only_recorded_when_kimi_accepted_the_cookie(world):  # noqa: F811
    engine, _ = world
    await _kimi_source(world, "user-1")

    def rejected(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    await _poll(ACCOUNT, rejected, "kimi_coding")

    assert _kimi_state(engine)[0].verified_subject is None


@pytest.mark.asyncio
async def test_the_subject_is_compared_for_the_polled_account_not_a_stranded_row(world):  # noqa: F811
    """A stale row for the same source under another account (inserted first, so an
    unscoped lookup finds it first) must not hold the baseline."""
    engine, _ = world
    with Session(engine) as session:
        session.add(
            CredentialSource(
                provider_id="kimi_coding",
                account_id="aaa-stranded-account",
                source_id=KIMI_SOURCE,
                source_type="cookie",
                source_label=SIDECAR,
                credential_origin=KIMI_ORIGIN,
                sidecar_id=SIDECAR,
            )
        )
        session.commit()
    await _kimi_source(world, "user-1")
    await _poll(ACCOUNT, _kimi, "kimi_coding")  # records user-1 for the polled account

    with Session(engine) as session:
        recorded = {
            row.account_id: row.verified_subject
            for row in session.exec(
                select(CredentialSource).where(CredentialSource.provider_id == "kimi_coding")
            ).all()
        }
    assert recorded == {ACCOUNT: "user-1", "aaa-stranded-account": None}

    await _swap_cookie(world, "user-2")
    assert await _poll(ACCOUNT, _kimi, "kimi_coding") == []


@pytest.mark.asyncio
async def test_the_subject_comparison_ignores_case(world):  # noqa: F811
    engine, _ = world
    await _kimi_source(world, "User-1")
    await _poll(ACCOUNT, _kimi, "kimi_coding")

    await _swap_cookie(world, "user-1")

    assert await _poll(ACCOUNT, _kimi, "kimi_coding")
    assert _kimi_state(engine)[1] is not None
