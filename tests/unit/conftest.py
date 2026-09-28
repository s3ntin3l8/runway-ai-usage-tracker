import asyncio

import pytest

from app.services import credential_alerts as credential_alerts_module
from app.services import webhooks as webhooks_module

# Phase 1 schema reset: these test files reference deleted DB models
# (UsageSnapshot, UsageSnapshotModel, CumulativeUsage) and will be
# rewritten in later phases. Excluded from collection until then.
collect_ignore = [
    "test_accumulator.py",
    "test_compaction.py",
    "test_db_token_fields.py",
    "test_history_deltas.py",
    "test_history_helpers.py",
    "test_new_db_schemas.py",
    "test_poller_tokens.py",
]


@pytest.fixture(autouse=True)
def _fresh_alert_locks(monkeypatch):
    """A module-level asyncio.Lock binds to whichever event loop first awaits
    it; pytest-asyncio spins up a fresh loop per test, so a lock left over
    from a previous test's loop raises "bound to a different event loop".
    Give every test its own unbound Lock for both check_credential_alerts's
    and check_and_fire's dedup locks — shared here (rather than duplicated
    per test file) so the two fixtures can't drift apart. Harmless no-op for
    tests that never touch either module.
    """
    monkeypatch.setattr(credential_alerts_module, "_check_lock", asyncio.Lock())
    monkeypatch.setattr(webhooks_module, "_fire_lock", asyncio.Lock())
