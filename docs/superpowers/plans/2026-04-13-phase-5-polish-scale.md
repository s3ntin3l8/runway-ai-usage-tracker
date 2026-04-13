# Phase 5 — Polish & Scale Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement 5C (smart polling sleep mode), 5B (CSV export + Discord/Slack webhook alerts), and 5A (Chart.js token volume charts on the History tab), in that order.

**Architecture:** 5C adds dormancy tracking to the `BackgroundPoller` with no new files. 5B adds a `WebhookConfig` SQLModel table, a `webhooks.py` service for breach detection, CRUD endpoints in `system.py`, and a Settings UI section. 5A adds Chart.js (CDN) and a new `charts.js` module wired into the History tab via `app.js`.

**Tech Stack:** Python `collections.deque`, `httpx` (already installed), Chart.js 4.x (CDN, no build), SQLModel

---

## File Map

| File | Action | Purpose |
|:---|:---|:---|
| `app/services/poller.py` | Modify | 5C: sleep/wake state machine |
| `app/models/db.py` | Modify | 5B: `WebhookConfig` table |
| `app/services/webhooks.py` | Create | 5B: breach detection + Discord/Slack fire |
| `app/api/endpoints/system.py` | Modify | 5B: webhook CRUD + test endpoints |
| `app/api/endpoints/usage.py` | Modify | 5B: CSV export via `format=csv` param |
| `frontend/index.html` | Modify | 5A+5B: Chart.js CDN, canvas, CSV button |
| `frontend/js/charts.js` | Create | 5A: Chart.js wrapper module |
| `frontend/js/app.js` | Modify | 5A+5B: call updateCharts, CSV button URL, webhook settings UI |
| `tests/unit/test_poller_sleep.py` | Create | 5C: sleep/wake unit tests |
| `tests/unit/test_webhooks.py` | Create | 5B: breach logic unit tests |
| `tests/integration/test_webhooks_api.py` | Create | 5B: CRUD integration tests |
| `tests/integration/test_csv_export.py` | Create | 5B: CSV endpoint integration test |

---

## Task 1: Smart Polling Sleep Mode (5C)

**Files:**
- Modify: `app/services/poller.py`
- Create: `tests/unit/test_poller_sleep.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_poller_sleep.py
import pytest
from collections import deque
from unittest.mock import AsyncMock, MagicMock, patch
from app.services.poller import BackgroundPoller


def _make_cards(used: float, provider="anthropic", account="acc1"):
    return [{
        "service_name": "Test",
        "icon": "T",
        "remaining": "50%",
        "unit": "tokens",
        "reset": "monthly",
        "health": "good",
        "pace": "ok",
        "detail": "",
        "provider_id": provider,
        "account_id": account,
        "used_value": used,
        "limit_value": 1000.0,
        "data_source": "oauth",
    }]


@pytest.mark.asyncio
async def test_interval_unchanged_after_one_poll():
    """One poll with same values doesn't trigger sleep."""
    p = BackgroundPoller(interval_seconds=900)
    cards = _make_cards(100.0)
    with patch("app.services.poller.manager") as mock_mgr, \
         patch("app.services.poller.Session"):
        mock_mgr.collect_all = AsyncMock(return_value=cards)
        await p.poll_now()
    assert p._interval == 900


@pytest.mark.asyncio
async def test_sleep_triggered_after_3_identical_polls():
    """3 consecutive polls with identical values switches to 2-hour interval."""
    p = BackgroundPoller(interval_seconds=900)
    cards = _make_cards(100.0)
    with patch("app.services.poller.manager") as mock_mgr, \
         patch("app.services.poller.Session"):
        mock_mgr.collect_all = AsyncMock(return_value=cards)
        for _ in range(3):
            await p.poll_now()
    assert p._interval == 7200


@pytest.mark.asyncio
async def test_wake_on_changed_value():
    """Changing used_value resets interval to base after sleep."""
    p = BackgroundPoller(interval_seconds=900)
    p._interval = 7200  # simulate sleeping
    # Seed hash deques with old identical values
    p._snapshot_hashes["anthropic:acc1"] = deque([hash((100.0, 1000.0))] * 3, maxlen=3)

    changed_cards = _make_cards(500.0)  # different value
    with patch("app.services.poller.manager") as mock_mgr, \
         patch("app.services.poller.Session"):
        mock_mgr.collect_all = AsyncMock(return_value=changed_cards)
        await p.poll_now()
    assert p._interval == 900


@pytest.mark.asyncio
async def test_cached_cards_excluded_from_sleep_tracking():
    """Cards with data_source='cache' don't count toward dormancy."""
    p = BackgroundPoller(interval_seconds=900)
    cards = [{
        "service_name": "Test",
        "icon": "T",
        "remaining": "50%",
        "unit": "tokens",
        "reset": "monthly",
        "health": "good",
        "pace": "ok",
        "detail": "",
        "provider_id": "anthropic",
        "account_id": "acc1",
        "used_value": 100.0,
        "limit_value": 1000.0,
        "data_source": "cache",  # should be excluded
    }]
    with patch("app.services.poller.manager") as mock_mgr, \
         patch("app.services.poller.Session"):
        mock_mgr.collect_all = AsyncMock(return_value=cards)
        for _ in range(5):  # more than 3 polls
            await p.poll_now()
    assert "anthropic:acc1" not in p._snapshot_hashes
    assert p._interval == 900  # no sleep triggered


@pytest.mark.asyncio
async def test_all_accounts_must_be_dormant_for_sleep():
    """Sleep only triggers when ALL accounts are dormant."""
    p = BackgroundPoller(interval_seconds=900)
    # acc1 is stable, acc2 is always changing
    call_count = 0

    async def varying_collect():
        nonlocal call_count
        call_count += 1
        return _make_cards(100.0) + [{
            "service_name": "Test2",
            "icon": "T",
            "remaining": "50%",
            "unit": "tokens",
            "reset": "monthly",
            "health": "good",
            "pace": "ok",
            "detail": "",
            "provider_id": "openai",
            "account_id": "acc2",
            "used_value": float(call_count * 10),  # always different
            "limit_value": 1000.0,
            "data_source": "oauth",
        }]

    with patch("app.services.poller.manager") as mock_mgr, \
         patch("app.services.poller.Session"):
        mock_mgr.collect_all = varying_collect
        for _ in range(3):
            await p.poll_now()
    assert p._interval == 900  # NOT sleeping — acc2 is always changing
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
source .venv/bin/activate && pytest tests/unit/test_poller_sleep.py -v 2>&1 | head -30
```

Expected: `AttributeError: 'BackgroundPoller' object has no attribute '_interval'`

- [ ] **Step 3: Update app/services/poller.py with sleep/wake logic**

Replace the entire file:

```python
# app/services/poller.py
import asyncio
import logging
from collections import deque
from datetime import datetime, timezone
from sqlmodel import Session
from app.services.collector_manager import manager
from app.core.db import engine
from app.models.db import UsageSnapshot
from app.models.schemas import LimitCard
from typing import List, Optional

logger = logging.getLogger(__name__)

_COMPACTION_INTERVAL_POLLS = 96   # 96 × 15 min ≈ 24 hours
_SLEEP_INTERVAL = 7200            # 2 hours in seconds
_DORMANT_THRESHOLD = 3            # consecutive identical polls before sleep


class BackgroundPoller:
    def __init__(self, interval_seconds: int = 900):
        self._base_interval = interval_seconds
        self._interval = interval_seconds   # current active interval
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._poll_count = 0
        self._snapshot_hashes: dict[str, deque] = {}  # key → deque(maxlen=3) of hashes

    def start(self):
        """Start the background polling task."""
        if self._task is not None:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(f"Background poller started with {self._base_interval}s interval.")

    async def stop(self):
        """Stop the background polling task."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Background poller stopped.")

    async def _run_loop(self):
        while self._running:
            await asyncio.sleep(self._interval)
            if not self._running:
                break
            try:
                await self.poll_now()
            except Exception as e:
                logger.error(f"Error during background poll: {e}")

    def _update_sleep_state(self, cards: list) -> None:
        """Track quota hashes per account; adjust poll interval on dormancy or wake."""
        for card_dict in cards:
            try:
                card = LimitCard(**card_dict)
                if not card.provider_id or not card.account_id:
                    continue
                if card.data_source == "cache":
                    continue  # cached cards don't represent fresh activity
                key = f"{card.provider_id}:{card.account_id}"
                if key not in self._snapshot_hashes:
                    self._snapshot_hashes[key] = deque(maxlen=_DORMANT_THRESHOLD)
                self._snapshot_hashes[key].append(hash((card.used_value, card.limit_value)))
            except Exception:
                pass  # malformed card — skip silently

        if not self._snapshot_hashes:
            return

        # Wake: any account's latest hash differs from previous
        any_changed = any(
            len(dq) >= 2 and dq[-1] != dq[-2]
            for dq in self._snapshot_hashes.values()
        )
        if any_changed:
            if self._interval != self._base_interval:
                logger.info("Activity detected — resuming normal polling interval")
                self._interval = self._base_interval
                self._snapshot_hashes.clear()
            return

        # Sleep: all accounts have been identical for _DORMANT_THRESHOLD polls
        all_dormant = all(
            len(dq) == _DORMANT_THRESHOLD and len(set(dq)) == 1
            for dq in self._snapshot_hashes.values()
        )
        if all_dormant and self._interval == self._base_interval:
            logger.info("No quota activity detected — entering sleep mode (2h interval)")
            self._interval = _SLEEP_INTERVAL

    async def poll_now(self):
        """Execute a single collection and snapshot cycle."""
        logger.info("Starting scheduled background collection...")
        cards = await manager.collect_all()

        # Update dormancy state before DB write
        self._update_sleep_state(cards)

        if not cards:
            logger.debug("No metrics collected during background poll.")
            return

        with Session(engine) as session:
            for card_dict in cards:
                try:
                    card = LimitCard(**card_dict)
                    if not card.provider_id or not card.account_id:
                        continue
                    if card.data_source == "cache":
                        continue
                    snapshot = UsageSnapshot(
                        provider_id=card.provider_id,
                        account_id=card.account_id,
                        account_label=card.account_label,
                        service_name=card.service_name,
                        used_value=card.used_value,
                        limit_value=card.limit_value,
                        unit_type=card.unit_type,
                        currency=card.currency,
                        tier=card.tier,
                        model_id=card.model_id,
                        window_type=card.window_type,
                        health=card.health,
                        sidecar_id=card.sidecar_id,
                        is_unlimited=card.is_unlimited,
                        data_source=card.data_source,
                        error_type=card.error_type,
                        timestamp=datetime.now(timezone.utc),
                    )
                    snapshot.raw_metadata = card.metadata
                    session.add(snapshot)
                except Exception as e:
                    logger.error(f"Failed to map card to snapshot: {e}")

            session.commit()
            logger.info(f"Background poll complete. Snapshotted {len(cards)} metrics.")

        # Daily compaction (every 96 polls ≈ 24h)
        self._poll_count += 1
        if self._poll_count % _COMPACTION_INTERVAL_POLLS == 0:
            try:
                from app.services.compaction import compact_snapshots
                with Session(engine) as compact_session:
                    result = compact_snapshots(compact_session)
                    logger.info(f"Daily compaction: {result}")
            except Exception as e:
                logger.error(f"Compaction failed (non-fatal): {e}")


# Global instance
poller = BackgroundPoller()
```

- [ ] **Step 4: Run sleep mode tests**

```bash
source .venv/bin/activate && pytest tests/unit/test_poller_sleep.py -v
```

Expected: all 5 tests PASS

- [ ] **Step 5: Run full suite**

```bash
source .venv/bin/activate && pytest -x --ignore=tests/unit/test_browser_cookies.py -q
```

Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add app/services/poller.py tests/unit/test_poller_sleep.py
git commit -m "feat(5C): smart polling sleep mode — 2h interval after 45min of no quota change"
```

---

## Task 2: CSV Export (5B Part 1)

**Files:**
- Modify: `app/api/endpoints/usage.py`
- Create: `tests/integration/test_csv_export.py`

- [ ] **Step 1: Write failing test**

```python
# tests/integration/test_csv_export.py
import pytest
from datetime import datetime, timezone
from fastapi.testclient import TestClient
from sqlmodel import Session, create_engine, SQLModel
from sqlmodel.pool import StaticPool

from app.main import app
from app.core.db import get_session
from app.models.db import UsageSnapshot


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


@pytest.fixture(name="client")
def client_fixture(session):
    app.dependency_overrides[get_session] = lambda: session
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


def _add_snapshot(session, provider="anthropic", used=100.0):
    snap = UsageSnapshot(
        timestamp=datetime.now(timezone.utc),
        provider_id=provider,
        account_id="acc1",
        service_name="Test",
        used_value=used,
        limit_value=1000.0,
        unit_type="tokens",
        health="good",
        data_source="oauth",
        window_type="monthly",
    )
    session.add(snap)
    session.commit()
    return snap


def test_csv_export_returns_csv_content_type(client, session):
    _add_snapshot(session)
    response = client.get("/api/v1/usage/history?format=csv")
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]


def test_csv_export_has_correct_headers(client, session):
    _add_snapshot(session)
    response = client.get("/api/v1/usage/history?format=csv")
    lines = response.text.strip().splitlines()
    header = lines[0]
    assert "timestamp" in header
    assert "provider_id" in header
    assert "used_value" in header
    assert "limit_value" in header
    assert "service_name" in header


def test_csv_export_contains_data_row(client, session):
    _add_snapshot(session, provider="openai", used=250.0)
    response = client.get("/api/v1/usage/history?format=csv")
    assert "openai" in response.text
    assert "250.0" in response.text


def test_csv_export_content_disposition(client, session):
    _add_snapshot(session)
    response = client.get("/api/v1/usage/history?format=csv")
    disposition = response.headers.get("content-disposition", "")
    assert "attachment" in disposition
    assert "runway-history-" in disposition
    assert ".csv" in disposition


def test_json_format_still_works(client, session):
    """Default (JSON) format is unaffected."""
    _add_snapshot(session)
    response = client.get("/api/v1/usage/history")
    assert response.status_code == 200
    assert isinstance(response.json(), list)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
source .venv/bin/activate && pytest tests/integration/test_csv_export.py -v 2>&1 | head -20
```

Expected: tests fail (no `format` param on the history endpoint yet)

- [ ] **Step 3: Update app/api/endpoints/usage.py**

Replace the file contents:

```python
# app/api/endpoints/usage.py
import csv
import io
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, Depends, Query, Request, HTTPException
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select, desc

from app.core.db import get_session
from app.core.rate_limit import limiter
from app.models.db import UsageSnapshot
from app.models.schemas import LimitsResponse, LimitCard
from app.services.collector_manager import manager

router = APIRouter()

_CSV_COLUMNS = [
    "timestamp", "provider_id", "account_id", "account_label", "service_name",
    "used_value", "limit_value", "unit_type", "currency", "tier", "model_id",
    "window_type", "health",
]


@router.get("/limits")
@limiter.limit("10/minute")
async def fetch_all_limits(request: Request) -> Dict[str, Any]:
    """Fetch all AI service usage limits from the in-memory registry."""
    results = manager.get_registry_snapshot()
    if not results:
        results = await manager.collect_all()
    limit_cards = [LimitCard(**item) for item in results]
    response = LimitsResponse(limits=limit_cards)
    return response.model_dump(exclude_none=False)


@router.get("/history")
@limiter.limit("30/minute")
async def get_usage_history(
    request: Request,
    provider_id: Optional[str] = None,
    account_id: Optional[str] = None,
    days: int = Query(default=7, ge=1, le=90),
    limit: int = Query(default=50, ge=1, le=500),
    format: str = Query(default="json"),
    session: Session = Depends(get_session),
):
    """Fetch usage history snapshots. Use format=csv for a downloadable CSV."""
    since = datetime.now(timezone.utc) - timedelta(days=days)

    statement = select(UsageSnapshot).where(UsageSnapshot.timestamp >= since)
    if provider_id:
        statement = statement.where(UsageSnapshot.provider_id == provider_id)
    if account_id:
        statement = statement.where(UsageSnapshot.account_id == account_id)
    statement = statement.order_by(desc(UsageSnapshot.timestamp)).limit(limit)

    results = session.exec(statement).all()

    if format == "csv":
        return _history_as_csv(results)

    return [_snapshot_to_dict(s) for s in results]


def _snapshot_to_dict(s: UsageSnapshot) -> dict:
    return {
        "id": s.id,
        "timestamp": s.timestamp.isoformat(),
        "provider_id": s.provider_id,
        "account_id": s.account_id,
        "account_label": s.account_label,
        "service_name": s.service_name,
        "used_value": s.used_value,
        "limit_value": s.limit_value,
        "unit_type": s.unit_type,
        "currency": s.currency,
        "tier": s.tier,
        "model_id": s.model_id,
        "window_type": s.window_type,
        "health": s.health,
        "sidecar_id": s.sidecar_id,
        "is_unlimited": s.is_unlimited,
        "data_source": s.data_source,
        "metadata": s.raw_metadata,
    }


def _history_as_csv(results: list) -> StreamingResponse:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=_CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for s in results:
        writer.writerow({
            "timestamp": s.timestamp.isoformat(),
            "provider_id": s.provider_id,
            "account_id": s.account_id,
            "account_label": s.account_label or "",
            "service_name": s.service_name,
            "used_value": s.used_value,
            "limit_value": s.limit_value,
            "unit_type": s.unit_type,
            "currency": s.currency or "",
            "tier": s.tier or "",
            "model_id": s.model_id or "",
            "window_type": s.window_type,
            "health": s.health,
        })
    filename = f"runway-history-{datetime.now().strftime('%Y-%m-%d')}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.post("/reset/{provider}")
@limiter.limit("10/minute")
async def reset_provider(
    request: Request, provider: str, account_id: Optional[str] = None
) -> Dict[str, Any]:
    """Reset terminal failure state for a provider."""
    if provider not in manager.collector_registry:
        raise HTTPException(status_code=404, detail=f"Provider '{provider}' not found")
    await manager.reset_collector(provider, account_id)
    return {"status": "reset", "provider": provider, "account_id": account_id}
```

- [ ] **Step 4: Run CSV tests**

```bash
source .venv/bin/activate && pytest tests/integration/test_csv_export.py -v
```

Expected: all 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add app/api/endpoints/usage.py tests/integration/test_csv_export.py
git commit -m "feat(5B): add CSV export to /api/v1/usage/history?format=csv"
```

---

## Task 3: WebhookConfig Model (5B Part 2)

**Files:**
- Modify: `app/models/db.py`

- [ ] **Step 1: Add WebhookConfig to app/models/db.py**

Append to the end of `app/models/db.py`:

```python
class WebhookConfig(SQLModel, table=True):
    """Per-provider webhook alert configuration."""
    __tablename__ = "webhook_configs"

    id: Optional[int] = Field(default=None, primary_key=True)
    provider_id: str  # provider name e.g. "anthropic", or "*" for global
    threshold_pct: float  # 0.0–100.0, e.g. 90.0
    url: str  # Discord or Slack incoming webhook URL
    channel: str  # "discord" or "slack"
    active: bool = Field(default=True)
    last_fired_at: Optional[datetime] = Field(default=None)  # None = reset/ready to fire
```

- [ ] **Step 2: Verify DB table is created on startup**

```bash
source .venv/bin/activate && python -c "
from app.core.db import init_db, engine
init_db()
from sqlmodel import inspect
insp = inspect(engine)
print('Tables:', insp.get_table_names())
"
```

Expected output includes `webhook_configs` in the table list.

- [ ] **Step 3: Commit**

```bash
git add app/models/db.py
git commit -m "feat(5B): add WebhookConfig SQLModel table"
```

---

## Task 4: Webhook Breach Detection Service (5B Part 3)

**Files:**
- Create: `app/services/webhooks.py`
- Create: `tests/unit/test_webhooks.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_webhooks.py
import pytest
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from sqlmodel import Session, create_engine, SQLModel
from sqlmodel.pool import StaticPool

from app.models.db import WebhookConfig
from app.models.schemas import LimitCard


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


def _card(provider="anthropic", used=950.0, limit=1000.0, account="acc1"):
    return LimitCard(
        service_name="Test",
        icon="T",
        remaining="5%",
        unit="tokens",
        reset="monthly",
        health="warning",
        pace="high",
        detail="",
        provider_id=provider,
        account_id=account,
        account_label="test@example.com",
        used_value=used,
        limit_value=limit,
        data_source="oauth",
    )


def _config(session, provider="anthropic", threshold=90.0, channel="discord",
            last_fired=None):
    cfg = WebhookConfig(
        provider_id=provider,
        threshold_pct=threshold,
        url="https://discord.example.com/webhook",
        channel=channel,
        active=True,
        last_fired_at=last_fired,
    )
    session.add(cfg)
    session.commit()
    session.refresh(cfg)
    return cfg


@pytest.mark.asyncio
async def test_fires_when_above_threshold(session):
    """Webhook fires when usage exceeds threshold and last_fired_at is None."""
    from app.services.webhooks import check_and_fire
    _config(session)  # threshold=90%, last_fired=None

    with patch("app.services.webhooks.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client

        await check_and_fire([_card(used=950.0, limit=1000.0)], session)  # 95% > 90%

        assert mock_client.post.called


@pytest.mark.asyncio
async def test_does_not_fire_when_below_threshold(session):
    """No webhook fired when usage is below threshold."""
    from app.services.webhooks import check_and_fire
    _config(session)  # threshold=90%

    with patch("app.services.webhooks.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock()
        mock_cls.return_value = mock_client

        await check_and_fire([_card(used=800.0, limit=1000.0)], session)  # 80% < 90%

        assert not mock_client.post.called


@pytest.mark.asyncio
async def test_does_not_refire_same_breach(session):
    """Once fired, does not fire again while still above threshold."""
    from app.services.webhooks import check_and_fire
    _config(session, last_fired=datetime.now(timezone.utc))  # already fired

    with patch("app.services.webhooks.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock()
        mock_cls.return_value = mock_client

        await check_and_fire([_card(used=950.0)], session)  # still above

        assert not mock_client.post.called


@pytest.mark.asyncio
async def test_resets_when_below_hysteresis(session):
    """last_fired_at cleared when usage drops below threshold * 0.85."""
    from app.services.webhooks import check_and_fire
    fired_time = datetime.now(timezone.utc)
    cfg = _config(session, threshold=90.0, last_fired=fired_time)

    with patch("app.services.webhooks.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock()
        mock_cls.return_value = mock_client

        # 70% < 90% * 0.85 = 76.5% → should reset
        await check_and_fire([_card(used=700.0, limit=1000.0)], session)

    session.refresh(cfg)
    assert cfg.last_fired_at is None


def test_discord_payload_has_embed(session):
    """Discord payload contains embeds array with correct color."""
    from app.services.webhooks import _discord_payload
    card = _card()
    payload = _discord_payload(card, 95.0, 90.0)
    assert "embeds" in payload
    assert payload["embeds"][0]["color"] == 0xED4245


def test_slack_payload_has_blocks(session):
    """Slack payload contains blocks array."""
    from app.services.webhooks import _slack_payload
    card = _card()
    payload = _slack_payload(card, 95.0, 90.0)
    assert "blocks" in payload
    assert payload["blocks"][0]["type"] == "header"


@pytest.mark.asyncio
async def test_global_wildcard_matches_all_providers(session):
    """provider_id='*' config fires for any provider card."""
    from app.services.webhooks import check_and_fire
    cfg = WebhookConfig(
        provider_id="*",
        threshold_pct=90.0,
        url="https://discord.example.com/webhook",
        channel="discord",
        active=True,
        last_fired_at=None,
    )
    session.add(cfg)
    session.commit()

    with patch("app.services.webhooks.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client

        await check_and_fire([_card(provider="openai", used=950.0)], session)

        assert mock_client.post.called
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
source .venv/bin/activate && pytest tests/unit/test_webhooks.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'app.services.webhooks'`

- [ ] **Step 3: Create app/services/webhooks.py**

```python
# app/services/webhooks.py
import logging
import httpx
from datetime import datetime, timezone
from sqlmodel import Session, select
from app.models.db import WebhookConfig
from app.models.schemas import LimitCard
from typing import Optional

logger = logging.getLogger(__name__)

_HYSTERESIS = 0.85  # reset alert when usage drops below threshold * 0.85


async def check_and_fire(cards: list[LimitCard], session: Session) -> None:
    """
    Check all active webhook configs against current card values.
    Fires when used_pct >= threshold and last_fired_at is None.
    Resets last_fired_at when usage drops below threshold * _HYSTERESIS.
    Provider-specific configs are evaluated before global '*' configs.
    """
    configs = session.exec(
        select(WebhookConfig).where(WebhookConfig.active == True)  # noqa: E712
    ).all()
    if not configs:
        return

    # Build provider → cards lookup
    card_by_provider: dict[str, list[LimitCard]] = {}
    for card in cards:
        if card.provider_id:
            card_by_provider.setdefault(card.provider_id, []).append(card)

    # Evaluate specific providers first, wildcards last
    sorted_configs = sorted(configs, key=lambda c: (c.provider_id == "*", c.id))

    async with httpx.AsyncClient(timeout=5.0) as client:
        for config in sorted_configs:
            if config.provider_id == "*":
                matched = [c for cards_list in card_by_provider.values() for c in cards_list]
            else:
                matched = card_by_provider.get(config.provider_id, [])

            for card in matched:
                if card.used_value is None or card.limit_value is None or card.limit_value == 0:
                    continue

                used_pct = (card.used_value / card.limit_value) * 100.0

                # Reset: usage recovered below hysteresis band
                if used_pct < config.threshold_pct * _HYSTERESIS:
                    if config.last_fired_at is not None:
                        config.last_fired_at = None
                        session.add(config)
                    continue

                # Fire: threshold crossed and no active breach recorded
                if used_pct >= config.threshold_pct and config.last_fired_at is None:
                    try:
                        await _fire_webhook(client, config, card, used_pct)
                        config.last_fired_at = datetime.now(timezone.utc)
                        session.add(config)
                    except Exception as e:
                        logger.error(f"Webhook delivery failed for config {config.id}: {e}")

    session.commit()


async def _fire_webhook(
    client: httpx.AsyncClient,
    config: WebhookConfig,
    card: LimitCard,
    used_pct: float,
) -> None:
    """Dispatch a single webhook notification."""
    if config.channel == "discord":
        payload = _discord_payload(card, used_pct, config.threshold_pct)
    else:
        payload = _slack_payload(card, used_pct, config.threshold_pct)
    response = await client.post(config.url, json=payload)
    response.raise_for_status()
    logger.info(f"Webhook fired: {config.provider_id} @ {used_pct:.1f}% (config {config.id})")


def _discord_payload(card: LimitCard, used_pct: float, threshold: float) -> dict:
    return {
        "embeds": [{
            "title": f"Quota Alert: {card.service_name}",
            "color": 0xED4245,
            "fields": [
                {"name": "Provider", "value": card.provider_id or "unknown", "inline": True},
                {"name": "Account", "value": card.account_label or card.account_id or "unknown", "inline": True},
                {"name": "Usage", "value": f"{used_pct:.1f}%", "inline": True},
                {"name": "Threshold", "value": f"{threshold:.0f}%", "inline": True},
            ],
            "footer": {"text": "Runway · quota alert"},
        }]
    }


def _slack_payload(card: LimitCard, used_pct: float, threshold: float) -> dict:
    return {
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"Quota Alert: {card.service_name}"},
            },
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"*Provider:* {card.provider_id}"},
                    {"type": "mrkdwn", "text": f"*Account:* {card.account_label or card.account_id}"},
                    {"type": "mrkdwn", "text": f"*Usage:* {used_pct:.1f}% (threshold: {threshold:.0f}%)"},
                ],
            },
        ]
    }
```

- [ ] **Step 4: Run webhook unit tests**

```bash
source .venv/bin/activate && pytest tests/unit/test_webhooks.py -v
```

Expected: all 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/webhooks.py tests/unit/test_webhooks.py
git commit -m "feat(5B): webhook breach detection service with Discord and Slack payloads"
```

---

## Task 5: Webhook CRUD API + Poller Integration (5B Part 4)

**Files:**
- Modify: `app/api/endpoints/system.py`
- Modify: `app/services/poller.py`
- Create: `tests/integration/test_webhooks_api.py`

- [ ] **Step 1: Write failing integration tests**

```python
# tests/integration/test_webhooks_api.py
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, create_engine, SQLModel
from sqlmodel.pool import StaticPool
from app.main import app
from app.core.db import get_session
from app.models.db import WebhookConfig


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


@pytest.fixture(name="client")
def client_fixture(session):
    app.dependency_overrides[get_session] = lambda: session
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


def test_list_webhooks_empty(client):
    response = client.get("/api/v1/system/webhooks")
    assert response.status_code == 200
    assert response.json() == {"webhooks": []}


def test_create_webhook(client):
    payload = {
        "provider_id": "anthropic",
        "threshold_pct": 90.0,
        "url": "https://discord.example.com/hook",
        "channel": "discord",
    }
    response = client.post("/api/v1/system/webhooks", json=payload)
    assert response.status_code == 201
    assert "id" in response.json()


def test_list_webhooks_after_create(client):
    payload = {
        "provider_id": "openai",
        "threshold_pct": 85.0,
        "url": "https://hooks.slack.com/example",
        "channel": "slack",
    }
    client.post("/api/v1/system/webhooks", json=payload)
    response = client.get("/api/v1/system/webhooks")
    webhooks = response.json()["webhooks"]
    assert len(webhooks) == 1
    assert webhooks[0]["provider_id"] == "openai"
    assert webhooks[0]["threshold_pct"] == 85.0


def test_patch_webhook(client):
    create_resp = client.post("/api/v1/system/webhooks", json={
        "provider_id": "anthropic", "threshold_pct": 90.0,
        "url": "https://discord.example.com/hook", "channel": "discord",
    })
    webhook_id = create_resp.json()["id"]

    patch_resp = client.patch(f"/api/v1/system/webhooks/{webhook_id}",
                              json={"threshold_pct": 75.0})
    assert patch_resp.status_code == 200

    list_resp = client.get("/api/v1/system/webhooks")
    assert list_resp.json()["webhooks"][0]["threshold_pct"] == 75.0


def test_delete_webhook(client):
    create_resp = client.post("/api/v1/system/webhooks", json={
        "provider_id": "anthropic", "threshold_pct": 90.0,
        "url": "https://discord.example.com/hook", "channel": "discord",
    })
    webhook_id = create_resp.json()["id"]

    del_resp = client.delete(f"/api/v1/system/webhooks/{webhook_id}")
    assert del_resp.status_code == 204

    list_resp = client.get("/api/v1/system/webhooks")
    assert list_resp.json()["webhooks"] == []


def test_patch_nonexistent_webhook(client):
    response = client.patch("/api/v1/system/webhooks/9999", json={"threshold_pct": 50.0})
    assert response.status_code == 404


def test_delete_nonexistent_webhook(client):
    response = client.delete("/api/v1/system/webhooks/9999")
    assert response.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
source .venv/bin/activate && pytest tests/integration/test_webhooks_api.py -v 2>&1 | head -20
```

Expected: 404s (routes don't exist yet)

- [ ] **Step 3: Add webhook endpoints to app/api/endpoints/system.py**

Add these imports at the top of the file (after existing imports):

```python
from pydantic import BaseModel
from sqlmodel import select
from app.models.db import WebhookConfig
from app.core.db import get_session
```

Then add these classes and endpoints after the existing `refresh_token` endpoint:

```python
class _WebhookCreate(BaseModel):
    provider_id: str
    threshold_pct: float
    url: str
    channel: str  # "discord" or "slack"
    active: bool = True


class _WebhookUpdate(BaseModel):
    threshold_pct: Optional[float] = None
    url: Optional[str] = None
    active: Optional[bool] = None


@router.get("/webhooks")
async def list_webhooks(session: Session = Depends(get_session)) -> dict:
    """List all webhook alert configurations."""
    configs = session.exec(select(WebhookConfig)).all()
    return {"webhooks": [
        {
            "id": c.id,
            "provider_id": c.provider_id,
            "threshold_pct": c.threshold_pct,
            "url": c.url,
            "channel": c.channel,
            "active": c.active,
            "last_fired_at": c.last_fired_at.isoformat() if c.last_fired_at else None,
        }
        for c in configs
    ]}


@router.post("/webhooks", status_code=201)
async def create_webhook(
    body: _WebhookCreate, session: Session = Depends(get_session)
) -> dict:
    """Create a webhook alert configuration."""
    config = WebhookConfig(**body.model_dump())
    session.add(config)
    session.commit()
    session.refresh(config)
    return {"id": config.id}


@router.patch("/webhooks/{webhook_id}")
async def update_webhook(
    webhook_id: int,
    body: _WebhookUpdate,
    session: Session = Depends(get_session),
) -> dict:
    """Update a webhook alert configuration."""
    config = session.get(WebhookConfig, webhook_id)
    if not config:
        raise HTTPException(status_code=404, detail="Webhook not found")
    for key, value in body.model_dump(exclude_none=True).items():
        setattr(config, key, value)
    session.add(config)
    session.commit()
    return {"status": "updated"}


@router.delete("/webhooks/{webhook_id}", status_code=204)
async def delete_webhook(webhook_id: int, session: Session = Depends(get_session)) -> None:
    """Delete a webhook alert configuration."""
    config = session.get(WebhookConfig, webhook_id)
    if not config:
        raise HTTPException(status_code=404, detail="Webhook not found")
    session.delete(config)
    session.commit()


@router.post("/webhooks/{webhook_id}/test")
async def test_webhook(
    request: Request,
    webhook_id: int,
    session: Session = Depends(get_session),
) -> dict:
    """Fire a test payload to the webhook URL immediately."""
    config = session.get(WebhookConfig, webhook_id)
    if not config:
        raise HTTPException(status_code=404, detail="Webhook not found")

    from app.services.webhooks import _fire_webhook
    test_card = LimitCard(
        service_name="Test Alert",
        icon="T",
        remaining="5%",
        unit="tokens",
        reset="monthly",
        health="warning",
        pace="high",
        detail="",
        provider_id=config.provider_id if config.provider_id != "*" else "test",
        account_id="test-account",
        account_label="Test Account",
        used_value=config.threshold_pct + 5,
        limit_value=100.0,
        data_source="test",
    )
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await _fire_webhook(client, config, test_card, config.threshold_pct + 5)
        return {"status": "sent"}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Webhook delivery failed: {e}")
```

Also add `import httpx` to the imports at the top of `system.py`, and add the `LimitCard` import:
```python
import httpx
from app.models.schemas import LimitCard
```

- [ ] **Step 4: Wire check_and_fire into the poller**

In `app/services/poller.py`, add the webhook call at the end of the `poll_now` method, after the DB write session closes:

```python
        # Fire webhook alerts for any threshold breaches
        try:
            from app.services.webhooks import check_and_fire
            limit_cards = []
            for card_dict in cards:
                try:
                    limit_cards.append(LimitCard(**card_dict))
                except Exception:
                    pass
            if limit_cards:
                with Session(engine) as webhook_session:
                    await check_and_fire(limit_cards, webhook_session)
        except Exception as e:
            logger.error(f"Webhook check failed (non-fatal): {e}")
```

This block goes just before the `self._poll_count += 1` line.

- [ ] **Step 5: Run integration tests**

```bash
source .venv/bin/activate && pytest tests/integration/test_webhooks_api.py -v
```

Expected: all 7 tests PASS

- [ ] **Step 6: Run full suite**

```bash
source .venv/bin/activate && pytest -x --ignore=tests/unit/test_browser_cookies.py -q
```

Expected: all tests pass

- [ ] **Step 7: Commit**

```bash
git add app/api/endpoints/system.py app/services/poller.py tests/integration/test_webhooks_api.py
git commit -m "feat(5B): webhook CRUD API, test endpoint, and poller breach detection integration"
```

---

## Task 6: Webhook Settings UI + CSV Download Button (5B Part 5)

**Files:**
- Modify: `frontend/index.html`
- Modify: `frontend/js/app.js`

This task is frontend-only — no new tests (UI rendering is verified visually).

- [ ] **Step 1: Add CSV download button to History view in index.html**

In `frontend/index.html`, replace the History section:

```html
<!-- History View -->
<section id="view-history" class="view hidden">
    <!-- Chart Panel (populated by 5A — canvas placeholders added now) -->
    <div id="history-chart-panel" class="glass-panel rounded-3xl p-6 mb-6 hidden">
        <div class="flex items-center justify-between mb-4">
            <h3 class="text-sm font-semibold text-zinc-400 uppercase tracking-wide">Token Volume</h3>
            <div class="flex gap-2">
                <button id="chart-view-bar" class="toggle-btn active" onclick="setChartView('bar')">Bar</button>
                <button id="chart-view-line" class="toggle-btn" onclick="setChartView('line')">Line</button>
            </div>
        </div>
        <div id="chart-empty" class="hidden text-zinc-500 italic text-sm py-8 text-center">No data for selected range.</div>
        <div id="chart-bar-wrap"><canvas id="chart-bar" height="200"></canvas></div>
        <div id="chart-line-wrap" class="hidden"><canvas id="chart-line" height="200"></canvas></div>
    </div>

    <!-- Header + CSV download -->
    <div class="flex justify-between items-center mb-4">
        <h2 class="text-xl font-bold text-zinc-100 flex items-center gap-2">
            <span class="text-blue-400">🕒</span> Usage History
        </h2>
        <a id="csv-download-btn"
           href="/api/v1/usage/history?format=csv"
           download
           class="toggle-btn text-sm">
            Download CSV
        </a>
    </div>

    <div class="glass-panel rounded-3xl p-8">
        <div id="history-content" class="overflow-x-auto">
            <p class="text-zinc-500 italic">No history data found.</p>
        </div>
    </div>
</section>
```

- [ ] **Step 2: Add webhook settings section in app.js**

In `frontend/js/app.js`, find where the Settings view content is rendered (search for `settings-content`) and add a `renderWebhookSettings()` function and a call to it.

Add this function anywhere in `app.js` (e.g., after existing settings rendering functions):

```javascript
async function renderWebhookSettings() {
    const container = document.getElementById('settings-extra');
    if (!container) return;

    let webhooks = [];
    try {
        const res = await fetch('/api/v1/system/webhooks');
        webhooks = (await res.json()).webhooks || [];
    } catch (e) { /* ignore */ }

    container.innerHTML = `
        <div class="mt-8 border-t border-zinc-800 pt-6">
            <div class="flex items-center justify-between mb-4">
                <h3 class="text-sm font-semibold text-zinc-300 uppercase tracking-wide">Webhook Alerts</h3>
                <button onclick="addWebhookRow()" class="toggle-btn text-xs">+ Add</button>
            </div>
            <div id="webhook-rows" class="space-y-3">
                ${webhooks.map(w => webhookRowHtml(w)).join('')}
            </div>
        </div>
    `;
}

function webhookRowHtml(w) {
    return `
        <div class="flex flex-wrap gap-2 items-center p-3 bg-zinc-900/50 rounded-xl" data-webhook-id="${w.id}">
            <input type="text" value="${w.provider_id}" placeholder="provider or *"
                   class="mono text-xs bg-zinc-800 border border-zinc-700 rounded px-2 py-1 w-24 text-zinc-200"
                   onchange="patchWebhook(${w.id}, 'provider_id', this.value)">
            <input type="number" value="${w.threshold_pct}" min="1" max="100" step="1"
                   class="mono text-xs bg-zinc-800 border border-zinc-700 rounded px-2 py-1 w-16 text-zinc-200"
                   onchange="patchWebhook(${w.id}, 'threshold_pct', parseFloat(this.value))">
            <span class="text-zinc-600 text-xs">%</span>
            <select class="mono text-xs bg-zinc-800 border border-zinc-700 rounded px-2 py-1 text-zinc-200"
                    onchange="patchWebhook(${w.id}, 'channel', this.value)">
                <option value="discord" ${w.channel === 'discord' ? 'selected' : ''}>Discord</option>
                <option value="slack" ${w.channel === 'slack' ? 'selected' : ''}>Slack</option>
            </select>
            <input type="url" value="${w.url}" placeholder="Webhook URL"
                   class="mono text-xs bg-zinc-800 border border-zinc-700 rounded px-2 py-1 flex-1 min-w-[180px] text-zinc-200"
                   onchange="patchWebhook(${w.id}, 'url', this.value)">
            <button onclick="testWebhook(${w.id})" class="toggle-btn text-xs">Test</button>
            <button onclick="deleteWebhook(${w.id})" class="toggle-btn text-xs text-red-400">✕</button>
        </div>
    `;
}

async function addWebhookRow() {
    const res = await fetch('/api/v1/system/webhooks', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({provider_id: '*', threshold_pct: 90, url: '', channel: 'discord'}),
    });
    if (res.ok) renderWebhookSettings();
}

async function patchWebhook(id, field, value) {
    await fetch(`/api/v1/system/webhooks/${id}`, {
        method: 'PATCH',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({[field]: value}),
    });
}

async function testWebhook(id) {
    const res = await fetch(`/api/v1/system/webhooks/${id}/test`, {method: 'POST'});
    const data = await res.json();
    alert(res.ok ? 'Test sent!' : `Failed: ${data.detail}`);
}

async function deleteWebhook(id) {
    await fetch(`/api/v1/system/webhooks/${id}`, {method: 'DELETE'});
    renderWebhookSettings();
}
```

Then ensure `renderWebhookSettings()` is called when the Settings view is shown. In the `switchView` function (or wherever settings content is loaded), add:

```javascript
if (view === 'settings') {
    renderWebhookSettings();
}
```

- [ ] **Step 3: Start the dev server and verify visually**

```bash
source .venv/bin/activate && uvicorn app.main:app --reload --port 8765
```

Open http://localhost:8765 in a browser:
1. Navigate to History → confirm "Download CSV" button appears and clicking it triggers a file download
2. Navigate to Settings → confirm "Webhook Alerts" section appears at the bottom with "+ Add" button
3. Click "+ Add" → confirm a new row appears with provider/threshold/channel/URL/Test/Delete controls

- [ ] **Step 4: Commit**

```bash
git add frontend/index.html frontend/js/app.js
git commit -m "feat(5B): CSV download button in History and Webhook Alerts section in Settings"
```

---

## Task 7: Chart.js Visualizations (5A)

**Files:**
- Modify: `frontend/index.html`
- Create: `frontend/js/charts.js`
- Modify: `frontend/js/app.js`

- [ ] **Step 1: Add Chart.js CDN to index.html**

In `frontend/index.html`, add this `<script>` tag just before the closing `</body>` tag (before the existing `app.js` script):

```html
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.2/dist/chart.umd.min.js"></script>
```

Also unhide the chart panel by removing the `hidden` class from `#history-chart-panel`:

```html
<div id="history-chart-panel" class="glass-panel rounded-3xl p-6 mb-6">
```

(Remove `hidden` from the class list.)

- [ ] **Step 2: Create frontend/js/charts.js**

```javascript
// frontend/js/charts.js
// Chart.js wrapper for the History tab token volume panel.
// Depends on Chart.js being loaded globally (CDN in index.html).

let _barChart = null;
let _lineChart = null;

const PROVIDER_COLORS = {
    anthropic: "#f59e0b",
    openai: "#10b981",
    google: "#3b82f6",
    github: "#8b5cf6",
    ollama: "#06b6d4",
    openrouter: "#ec4899",
    minimax: "#14b8a6",
};

function colorFor(providerId) {
    return PROVIDER_COLORS[providerId] || "#6b7280";
}

function bucketByDay(snapshots) {
    // Returns { "YYYY-MM-DD": { provider_id: { sum, count } } }
    const buckets = {};
    for (const snap of snapshots) {
        if (snap.used_value == null) continue;
        const day = snap.timestamp.slice(0, 10);
        if (!buckets[day]) buckets[day] = {};
        const pid = snap.provider_id || "unknown";
        if (!buckets[day][pid]) buckets[day][pid] = { sum: 0, count: 0 };
        buckets[day][pid].sum += snap.used_value;
        buckets[day][pid].count += 1;
    }
    return buckets;
}

function extractLabelsAndProviders(snapshots) {
    const days = new Set();
    const providers = new Set();
    for (const s of snapshots) {
        days.add(s.timestamp.slice(0, 10));
        if (s.provider_id) providers.add(s.provider_id);
    }
    return { labels: Array.from(days).sort(), providers: Array.from(providers) };
}

function latestLimitPerProvider(snapshots) {
    const limits = {};
    for (const s of snapshots) {
        if (s.provider_id && s.limit_value != null) {
            limits[s.provider_id] = s.limit_value;
        }
    }
    return limits;
}

export function destroyCharts() {
    if (_barChart) { _barChart.destroy(); _barChart = null; }
    if (_lineChart) { _lineChart.destroy(); _lineChart = null; }
}

export function updateCharts(snapshots, activeView = "bar") {
    destroyCharts();

    const barCanvas = document.getElementById("chart-bar");
    const lineCanvas = document.getElementById("chart-line");
    const emptyEl = document.getElementById("chart-empty");
    if (!barCanvas || !lineCanvas) return;

    if (!snapshots || snapshots.length === 0) {
        emptyEl?.classList.remove("hidden");
        return;
    }
    emptyEl?.classList.add("hidden");

    const { labels, providers } = extractLabelsAndProviders(snapshots);
    const buckets = bucketByDay(snapshots);
    const limits = latestLimitPerProvider(snapshots);

    // --- Bar chart (stacked by provider) ---
    const barDatasets = providers.map(provider => ({
        label: provider,
        data: labels.map(day => {
            const b = buckets[day]?.[provider];
            return b ? Math.round(b.sum / b.count) : 0;
        }),
        backgroundColor: colorFor(provider),
        stack: "combined",
        borderRadius: 2,
    }));

    const chartDefaults = {
        responsive: true,
        animation: false,
        plugins: {
            legend: { labels: { color: "#a1a1aa", font: { size: 11 } } },
            tooltip: { mode: "index", intersect: false },
        },
        scales: {
            x: {
                stacked: true,
                ticks: { color: "#71717a", maxTicksLimit: 10 },
                grid: { color: "#27272a" },
            },
            y: {
                stacked: true,
                ticks: { color: "#71717a" },
                grid: { color: "#27272a" },
            },
        },
    };

    _barChart = new Chart(barCanvas.getContext("2d"), {
        type: "bar",
        data: { labels, datasets: barDatasets },
        options: { ...chartDefaults },
    });

    // --- Line chart (per provider + limit reference line) ---
    const lineDatasets = providers.flatMap(provider => {
        const color = colorFor(provider);
        const datasets = [{
            label: provider,
            data: labels.map(day => {
                const b = buckets[day]?.[provider];
                return b ? Math.round(b.sum / b.count) : null;
            }),
            borderColor: color,
            backgroundColor: color + "22",
            tension: 0.3,
            spanGaps: true,
            pointRadius: 3,
        }];
        if (limits[provider]) {
            datasets.push({
                label: `${provider} limit`,
                data: labels.map(() => limits[provider]),
                borderColor: color,
                borderDash: [6, 3],
                pointRadius: 0,
                tension: 0,
                fill: false,
            });
        }
        return datasets;
    });

    const lineOptions = {
        responsive: true,
        animation: false,
        plugins: {
            legend: { labels: { color: "#a1a1aa", font: { size: 11 } } },
        },
        scales: {
            x: { ticks: { color: "#71717a", maxTicksLimit: 10 }, grid: { color: "#27272a" } },
            y: { ticks: { color: "#71717a" }, grid: { color: "#27272a" } },
        },
    };

    _lineChart = new Chart(lineCanvas.getContext("2d"), {
        type: "line",
        data: { labels, datasets: lineDatasets },
        options: lineOptions,
    });

    // Show the active view
    setChartViewVisibility(activeView);
}

function setChartViewVisibility(view) {
    const barWrap = document.getElementById("chart-bar-wrap");
    const lineWrap = document.getElementById("chart-line-wrap");
    const barBtn = document.getElementById("chart-view-bar");
    const lineBtn = document.getElementById("chart-view-line");
    if (view === "bar") {
        barWrap?.classList.remove("hidden");
        lineWrap?.classList.add("hidden");
        barBtn?.classList.add("active");
        lineBtn?.classList.remove("active");
    } else {
        barWrap?.classList.add("hidden");
        lineWrap?.classList.remove("hidden");
        barBtn?.classList.remove("active");
        lineBtn?.classList.add("active");
    }
}

export function setChartView(view) {
    setChartViewVisibility(view);
}
```

- [ ] **Step 3: Wire charts.js into app.js**

In `frontend/js/app.js`, add the import at the top of the file:

```javascript
import { updateCharts, setChartView, destroyCharts } from '/static/js/charts.js';
```

Find the function that loads history data (search for `history-content` or `loadHistory` or similar) and add a `updateCharts(historyData, STATE.chartView || 'bar')` call after the history data is parsed.

Also add a `setChartView` global wrapper so the inline `onclick` in HTML can call it:

```javascript
window.setChartView = function(view) {
    STATE.chartView = view;
    setChartView(view);
};
```

- [ ] **Step 4: Start dev server and verify charts render**

```bash
source .venv/bin/activate && uvicorn app.main:app --reload --port 8765
```

Open http://localhost:8765 and navigate to History:
1. Confirm the chart panel appears above the history table
2. Confirm stacked bar chart renders with colored bars per provider (if data exists)
3. Click "Line" toggle → confirm line chart appears with dashed limit reference lines
4. Click "Bar" toggle → confirm switch back works
5. Confirm empty state message shows when no data available

- [ ] **Step 5: Run full test suite**

```bash
source .venv/bin/activate && pytest --ignore=tests/unit/test_browser_cookies.py -q
```

Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add frontend/index.html frontend/js/charts.js frontend/js/app.js
git commit -m "feat(5A): Chart.js token volume charts on History tab (stacked bar + line toggle)"
```

---

## Task 8: Final Phase 5 Verification & Completion

- [ ] **Step 1: Run the complete test suite**

```bash
source .venv/bin/activate && pytest --ignore=tests/unit/test_browser_cookies.py -v
```

Expected: all tests pass

- [ ] **Step 2: End-to-end smoke test**

Start the server and verify all three features work together:

```bash
source .venv/bin/activate && uvicorn app.main:app --reload --port 8765
```

- History tab: charts render, CSV download works, bar/line toggle works
- Settings tab: Webhook Alerts section present, "+ Add" creates a row, Test fires to configured URL
- Background: Check logs confirm sleep mode engages after 45min of unchanged quotas

- [ ] **Step 3: Update ideas.md to mark Phase 5 complete**

In `docs/ideas.md`, update Phase 5 sub-phases to `✅ Complete (2026-04-13)` for 5A, 5B, 5C.

- [ ] **Step 4: Final commit**

```bash
git add docs/ideas.md
git commit -m "Phase 5: smart polling, CSV export, webhook alerts, and Chart.js visualizations complete"
```
