"""The identity verifier backs off sources that cannot prove their account (#469)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlmodel import Session, select

from app.api.endpoints.fleet import _bundle_holds
from app.models.db import PendingCredentialTag
from app.services.collector_manager import MAX_VERIFICATIONS_PER_CYCLE
from app.services.credential_tags import PendingCredentialTagRepo
from tests.integration.test_pending_identity_verification import (
    BOB,
    CHATGPT_COOKIE,
    SIDECAR,
    _chatgpt,
    _seed,
    _verify,
    world,  # noqa: F401 — fixture
)

COOKIE_KEY = "cookie___Secure-next-auth.session-token"


def _origin(n: int = 0) -> str:
    return f"cookie:chatgpt/session{n}"


def _row(engine, origin: str) -> PendingCredentialTag | None:
    with Session(engine) as session:
        return session.exec(
            select(PendingCredentialTag).where(PendingCredentialTag.credential_origin == origin)
        ).first()


def _make_due(engine, origin: str | None = None) -> None:
    with Session(engine) as session:
        rows = session.exec(select(PendingCredentialTag)).all()
        for row in rows:
            if origin is None or row.credential_origin == origin:
                row.next_verify_at = datetime.now(UTC) - timedelta(seconds=1)
                session.add(row)
        session.commit()


def _usage_calls(seen: list[httpx.Request]) -> int:
    return sum(1 for r in seen if r.url.path == "/backend-api/wham/usage")


def _aware(value: datetime | None) -> datetime:
    assert value is not None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def test_backoff_doubles_from_fifteen_minutes_and_caps_at_six_hours():
    backoff = PendingCredentialTagRepo.verification_backoff
    assert [backoff(n) for n in (1, 2, 3)] == [
        timedelta(minutes=15),
        timedelta(minutes=30),
        timedelta(minutes=60),
    ]
    assert backoff(5) == timedelta(hours=4)
    assert backoff(6) == timedelta(hours=6)
    assert backoff(40) == timedelta(hours=6)


@pytest.mark.asyncio
async def test_an_unidentifiable_source_is_not_called_again_until_its_backoff_elapses(world):  # noqa: F811
    engine, _ = world
    await _seed(world, "chatgpt", _origin(), {COOKIE_KEY: CHATGPT_COOKIE})
    seen: list[httpx.Request] = []
    handler = _chatgpt(seen, email=None)  # works, but never names its account

    await _verify("chatgpt", handler)
    assert _usage_calls(seen) == 1
    row = _row(engine, _origin())
    assert row is not None and row.verify_attempts == 1
    wait = _aware(row.next_verify_at) - datetime.now(UTC)
    assert timedelta(minutes=14) < wait <= timedelta(minutes=15)

    await _verify("chatgpt", handler)
    assert _usage_calls(seen) == 1  # still backing off

    _make_due(engine)
    await _verify("chatgpt", handler)
    assert _usage_calls(seen) == 2
    row = _row(engine, _origin())
    assert row is not None and row.verify_attempts == 2
    wait = _aware(row.next_verify_at) - datetime.now(UTC)
    assert timedelta(minutes=29) < wait <= timedelta(minutes=30)


@pytest.mark.asyncio
async def test_a_failing_source_backs_off_too(world):  # noqa: F811
    engine, _ = world
    await _seed(world, "chatgpt", _origin(), {COOKIE_KEY: CHATGPT_COOKIE})
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(401)

    await _verify("chatgpt", handler)
    first = len(seen)
    assert first > 0
    row = _row(engine, _origin())
    assert row is not None and row.verify_attempts == 1

    await _verify("chatgpt", handler)
    assert len(seen) == first


@pytest.mark.asyncio
async def test_at_most_five_sources_are_verified_per_cycle_never_tried_first(world):  # noqa: F811
    engine, _ = world
    total = MAX_VERIFICATIONS_PER_CYCLE + 2
    for n in range(total):
        await _seed(world, "chatgpt", _origin(n), {COOKIE_KEY: f"{CHATGPT_COOKIE}-{n}"})
    seen: list[httpx.Request] = []
    handler = _chatgpt(seen, email=None)

    await _verify("chatgpt", handler)
    assert _usage_calls(seen) == MAX_VERIFICATIONS_PER_CYCLE

    # The five tried are backing off; the two the cap left out go next.
    await _verify("chatgpt", handler)
    assert _usage_calls(seen) == total
    tried = [r for r in (_row(engine, _origin(n)) for n in range(total)) if r]
    assert all(r.verify_attempts == 1 for r in tried)


@pytest.mark.asyncio
async def test_an_identified_source_leaves_no_retry_state(world):  # noqa: F811
    engine, _ = world
    await _seed(world, "chatgpt", _origin(), {COOKIE_KEY: CHATGPT_COOKIE})

    await _verify("chatgpt", _chatgpt([], email=BOB))

    assert _row(engine, _origin()) is None


@pytest.mark.asyncio
async def test_a_new_secret_resets_the_backoff_but_the_same_secret_does_not(world):  # noqa: F811
    engine, cache = world
    source_id = await _seed(world, "chatgpt", _origin(), {COOKIE_KEY: CHATGPT_COOKIE})
    await _verify("chatgpt", _chatgpt([], email=None))
    row = _row(engine, _origin())
    assert row is not None and row.verify_attempts == 1

    # The sidecar re-pushes the very same bundle every cycle: no reset.
    assert await _bundle_holds("chatgpt", "default", source_id, {COOKIE_KEY: CHATGPT_COOKIE})
    # A re-login holds a different value: the ingest path resets the row.
    assert not await _bundle_holds("chatgpt", "default", source_id, {COOKIE_KEY: "relogin"})
    with Session(engine) as session:
        PendingCredentialTagRepo.reset_verification(
            session, sidecar_id=SIDECAR, provider_id="chatgpt", credential_origin=_origin()
        )
        session.commit()
    row = _row(engine, _origin())
    assert row is not None and row.verify_attempts == 0 and row.next_verify_at is None


@pytest.mark.asyncio
async def test_a_source_that_was_never_tried_goes_before_one_whose_backoff_just_elapsed(world):  # noqa: F811
    engine, _ = world
    total = MAX_VERIFICATIONS_PER_CYCLE + 2
    for n in range(total):
        await _seed(world, "chatgpt", _origin(n), {COOKIE_KEY: f"{CHATGPT_COOKIE}-{n}"})
    # The two lowest-sorting sources were tried before and are due again; the rest never were.
    with Session(engine) as session:
        for n in (0, 1):
            row = session.exec(
                select(PendingCredentialTag).where(
                    PendingCredentialTag.credential_origin == _origin(n)
                )
            ).one()
            row.next_verify_at = datetime.now(UTC) - timedelta(hours=n + 1)
            session.add(row)
        session.commit()

    await _verify("chatgpt", _chatgpt([], email=None))

    attempts = {
        n: (_row(engine, _origin(n)) or PendingCredentialTag).verify_attempts for n in range(total)
    }
    assert [n for n, count in attempts.items() if count] == list(range(2, total))


@pytest.mark.asyncio
async def test_sources_with_no_pending_row_still_back_off_instead_of_starving_the_rest(world):  # noqa: F811
    engine, _ = world
    total = MAX_VERIFICATIONS_PER_CYCLE + 2
    for n in range(total):
        await _seed(world, "chatgpt", _origin(n), {COOKIE_KEY: f"{CHATGPT_COOKIE}-{n}"})
    with Session(engine) as session:  # the manifest never listed (or has since dropped) them
        for row in session.exec(select(PendingCredentialTag)).all():
            session.delete(row)
        session.commit()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(401)  # fails: no preview write creates a row as a side effect

    def exchanges() -> int:
        return sum(1 for r in seen if r.url.path == "/api/auth/session")

    await _verify("chatgpt", handler)
    assert exchanges() == MAX_VERIFICATIONS_PER_CYCLE
    await _verify("chatgpt", handler)

    assert exchanges() == total  # the second cycle reached the two the cap left out


@pytest.mark.asyncio
async def test_a_rotating_access_token_under_one_refresh_token_is_not_a_new_secret(world):  # noqa: F811
    _, cache = world
    source_id = await _seed(
        world, "chatgpt", _origin(), {"refresh_token": "rt-1", "access_token": "at-1"}
    )

    assert await _bundle_holds(
        "chatgpt",
        "default",
        source_id,
        {"refresh_token": "rt-1", "access_token": "at-2", "expiry_date": "9"},
    )
    assert not await _bundle_holds(
        "chatgpt", "default", source_id, {"refresh_token": "rt-2", "access_token": "at-1"}
    )


@pytest.mark.asyncio
async def test_one_failing_backoff_write_does_not_lose_the_others(world, monkeypatch):  # noqa: F811
    engine, _ = world
    for n in range(3):
        await _seed(world, "chatgpt", _origin(n), {COOKIE_KEY: f"{CHATGPT_COOKIE}-{n}"})
    real = PendingCredentialTagRepo.record_verify_attempt

    def flaky(session, **kwargs):
        if kwargs["credential_origin"] == _origin(0):
            raise RuntimeError("database is locked")
        return real(session, **kwargs)

    monkeypatch.setattr(PendingCredentialTagRepo, "record_verify_attempt", staticmethod(flaky))

    await _verify("chatgpt", _chatgpt([], email=None))

    assert [
        (_row(engine, _origin(n)) or PendingCredentialTag()).verify_attempts for n in range(3)
    ] == [0, 1, 1]
