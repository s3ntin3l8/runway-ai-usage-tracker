"""Integration test for scripts/normalize_sidecar_ids.py.

Seeds a DB where one host registered under both its `.local` and FQDN names,
runs the migration, and asserts the two are folded onto the normalized `macbook`
id across every table — while an unrelated bare-named sidecar (`dev-01`) and the
all-sidecars (`""`) aggregate rows are left untouched, and account totals are
preserved.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.models.db import (
    LatestUsage,
    SidecarRegistry,
    UsageEvent,
    UsagePeriodRollup,
    UsageWindow,
)
from app.services.pricing_seed import seed_pricing_table

WEND = datetime(2026, 5, 9, tzinfo=UTC)
WSTART = datetime(2026, 5, 8, tzinfo=UTC)
NOW = datetime(2026, 5, 8, 14, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def mock_db_session():
    """Override the conftest autouse Session mock — this test needs a real DB.

    The migration constructs its own ``Session(engine)``, so the global
    ``sqlmodel.Session`` patch would otherwise hand it a no-op mock.
    """
    yield


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(eng)
    with Session(eng) as s:
        seed_pricing_table(s)
        s.commit()
    return eng


def _event(sidecar_id: str, event_id: str) -> UsageEvent:
    return UsageEvent(
        provider_id="anthropic",
        account_id="u@x",
        sidecar_id=sidecar_id,
        event_id=event_id,
        ts=NOW,
        kind="message",
        model_id="sonnet",
        tokens_input=100,
        tokens_output=50,
        cost_usd=0.01,
    )


def _window(sidecar_id: str, msgs: int) -> UsageWindow:
    return UsageWindow(
        provider_id="anthropic",
        account_id="u@x",
        window_type="weekly",
        window_start=WSTART,
        window_end=WEND,
        model_id="sonnet",
        sidecar_id=sidecar_id,
        msgs=msgs,
        tokens_input=100 * msgs,
        tokens_output=50 * msgs,
        cost_usd=0.01 * msgs,
    )


def _seed(engine):
    with Session(engine) as s:
        # Events: two under .local, one under the FQDN, one under a bare name.
        s.add(_event("macbook.local", "m1"))
        s.add(_event("macbook.local", "m2"))
        s.add(_event("Macbook.in.s3ntin3l8.de", "m3"))
        s.add(_event("dev-01", "d1"))

        # Live card tagged with the stale .local name.
        s.add(
            LatestUsage(
                provider_id="anthropic",
                account_id="u@x",
                window_type="weekly",
                variant="",
                model_id="sonnet",
                sidecar_id="macbook.local",
                card_json="{}",
            )
        )

        # Closed-window archive: a colliding pair (same grain, two host names) +
        # an all-sidecars row and a dev-01 row that must stay put.
        s.add(_window("macbook.local", 2))
        s.add(_window("Macbook.in.s3ntin3l8.de", 1))
        s.add(_window("", 3))
        s.add(_window("dev-01", 1))

        # Two registry rows for the same machine + one bare-named sidecar.
        s.add(
            SidecarRegistry(
                sidecar_id="macbook.local",
                hostname="macbook.local",
                first_seen=NOW - timedelta(days=10),
                last_seen=NOW - timedelta(days=2),
                ingest_count=5,
                error_count=1,
                sidecar_version="2.2.0",
                custom_name="My Mac",
            )
        )
        s.add(
            SidecarRegistry(
                sidecar_id="Macbook.in.s3ntin3l8.de",
                hostname="Macbook.in.s3ntin3l8.de",
                first_seen=NOW - timedelta(days=1),
                last_seen=NOW,
                ingest_count=3,
                error_count=0,
                sidecar_version="2.3.0",
            )
        )
        s.add(
            SidecarRegistry(
                sidecar_id="dev-01",
                hostname="dev-01",
                first_seen=NOW,
                last_seen=NOW,
                ingest_count=7,
            )
        )
        s.commit()


def test_migration_folds_macbook_and_leaves_others(engine):
    _seed(engine)

    # Point both the migration and the rollup-rebuild it calls at the test DB.
    with (
        patch("scripts.normalize_sidecar_ids.engine", engine),
        patch("scripts.backfill_rollups.engine", engine),
    ):
        from scripts.normalize_sidecar_ids import migrate

        assert migrate(apply=True) == 0

    with Session(engine) as s:
        # Events: macbook.local + FQDN now under "macbook"; dev-01 untouched.
        by_sidecar = {}
        for ev in s.exec(select(UsageEvent)).all():
            by_sidecar.setdefault(ev.sidecar_id, 0)
            by_sidecar[ev.sidecar_id] += 1
        assert by_sidecar == {"macbook": 3, "dev-01": 1}

        # Live card retagged.
        card = s.exec(select(LatestUsage)).one()
        assert card.sidecar_id == "macbook"

        # Windows: the colliding pair folded (2 + 1 = 3 msgs) under "macbook";
        # the all-sidecars ("") and dev-01 rows are untouched.
        wins = {w.sidecar_id: w for w in s.exec(select(UsageWindow)).all()}
        assert set(wins) == {"macbook", "", "dev-01"}
        assert wins["macbook"].msgs == 3
        assert wins["macbook"].tokens_input == 300
        assert wins[""].msgs == 3
        assert wins["dev-01"].msgs == 1

        # Registry: one merged "macbook" + the untouched dev-01.
        regs = {r.sidecar_id: r for r in s.exec(select(SidecarRegistry)).all()}
        assert set(regs) == {"macbook", "dev-01"}
        mac = regs["macbook"]
        assert mac.ingest_count == 8  # 5 + 3
        assert mac.error_count == 1
        # SQLite round-trips these tz-naive; compare on wall-clock.
        assert mac.first_seen.replace(tzinfo=None) == (NOW - timedelta(days=10)).replace(
            tzinfo=None
        )  # earliest
        assert mac.last_seen.replace(tzinfo=None) == NOW.replace(tzinfo=None)  # latest
        assert mac.sidecar_version == "2.3.0"  # from the latest-seen row
        assert mac.custom_name == "My Mac"  # preserved
        assert regs["dev-01"].ingest_count == 7

        # Rollups rebuilt: per-sidecar grain now under "macbook", none stale, and
        # the all-up lifetime total still counts every event (account total kept).
        rollup_sidecars = {r.sidecar_id for r in s.exec(select(UsagePeriodRollup)).all()}
        assert "macbook" in rollup_sidecars
        assert "macbook.local" not in rollup_sidecars
        assert "Macbook.in.s3ntin3l8.de" not in rollup_sidecars

        lifetime = s.exec(
            select(UsagePeriodRollup).where(
                UsagePeriodRollup.period_type == "lifetime",
                UsagePeriodRollup.model_id == "",
                UsagePeriodRollup.sidecar_id == "",
            )
        ).one()
        assert lifetime.msgs == 4  # all events still counted


def test_migration_noop_when_already_normalized(engine):
    with Session(engine) as s:
        s.add(SidecarRegistry(sidecar_id="dev-01", hostname="dev-01", ingest_count=1))
        s.commit()

    with (
        patch("scripts.normalize_sidecar_ids.engine", engine),
        patch("scripts.backfill_rollups.engine", engine),
    ):
        from scripts.normalize_sidecar_ids import migrate

        assert migrate(apply=True) == 0

    with Session(engine) as s:
        regs = s.exec(select(SidecarRegistry.sidecar_id)).all()
        assert regs == ["dev-01"]
