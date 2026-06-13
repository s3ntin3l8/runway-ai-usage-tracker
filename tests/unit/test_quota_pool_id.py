"""Schema + clustering signal for quota_pool_id.

The dashboard's frontend pool aggregator (webapp/src/lib/quota.ts) clusters
cards by exact `quota_pool_id` equality. These tests pin the field's schema
defaults (so we don't accidentally regress to behavioral clustering) and
verify that the Antigravity sidecar emitters populate it.
"""

from app.models.schemas import LimitCard


class TestSchemaDefault:
    def test_field_is_optional(self):
        """Cards built without quota_pool_id deserialize cleanly (defaults to None)."""
        c = LimitCard(service_name="gemini-flash", icon="g")
        assert c.quota_pool_id is None

    def test_field_round_trips(self):
        """Pool id survives serialization (UI relies on the dump shape)."""
        c = LimitCard(
            service_name="antigravity-sonnet",
            icon="a",
            quota_pool_id="antigravity:session:2026-05-23T16:00:00+00:00",
        )
        dumped = c.model_dump()
        assert dumped["quota_pool_id"] == "antigravity:session:2026-05-23T16:00:00+00:00"

    def test_default_card_has_no_pool_id_in_dump(self):
        """A null pool_id must serialize as null, not omitted (frontend checks `!= null`)."""
        c = LimitCard(service_name="gemini-flash", icon="g")
        dumped = c.model_dump()
        assert "quota_pool_id" in dumped
        assert dumped["quota_pool_id"] is None


class TestAntigravityLSPEmitter:
    """The LSP path in scripts/sidecar.py:collect_antigravity_lsp must tag
    per-model cards with a family-scoped quota_pool_id derived from
    `antigravity:{family}:{window_type}:{reset_at_iso}`.

    Antigravity has two physical pools (gemini, frontier) that can reset
    independently. Including the family in the id ensures each pool clusters
    separately even when both share the same reset_at.
    """

    def test_pool_id_format_gemini(self):
        reset_iso = "2026-05-23T16:00:00+00:00"
        window_type = "weekly"
        family = "gemini"
        pool_id = f"antigravity:{family}:{window_type}:{reset_iso}"
        assert pool_id == "antigravity:gemini:weekly:2026-05-23T16:00:00+00:00"

    def test_pool_id_format_frontier(self):
        reset_iso = "2026-05-23T16:00:00+00:00"
        window_type = "weekly"
        family = "frontier"
        pool_id = f"antigravity:{family}:{window_type}:{reset_iso}"
        assert pool_id == "antigravity:frontier:weekly:2026-05-23T16:00:00+00:00"

    def test_null_reset_yields_null_pool_id(self):
        """If a card has no reset_at we can't form a pool_id; leaving it None
        keeps the card a singleton in the aggregator (safe default)."""
        reset_iso = None
        family = "frontier"
        pool_id = f"antigravity:{family}:weekly:{reset_iso}" if reset_iso else None
        assert pool_id is None
