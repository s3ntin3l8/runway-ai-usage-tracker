"""Tests for the _pick_winner tie-break logic in collapse_default_account_events.

_pick_winner is pure (no DB), so these build in-memory UsageEvent rows and
assert which one the migration would keep. See the script's module docstring
for why each tier is ordered the way it is — derived from a breakdown of the
actual duplicate patterns found in production data.
"""

from datetime import UTC, datetime

from app.models.db import UsageEvent
from scripts.collapse_default_account_events import _pick_winner

_TS = datetime(2026, 7, 1, tzinfo=UTC)


def _ev(
    *,
    eid: int,
    account_id: str,
    kind: str = "message",
    tokens_input: int = 0,
    tokens_output: int = 0,
    ts: datetime = _TS,
) -> UsageEvent:
    return UsageEvent(
        id=eid,
        provider_id="opencode",
        account_id=account_id,
        event_id="msg_shared",
        ts=ts,
        kind=kind,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
    )


def test_message_beats_error_regardless_of_tokens():
    """kind is the top tier — even a zero-token message beats a nonzero-token
    error row (matches the 9 observed opencode pairs: default=error/0 vs
    email=message/0 — but the rule must hold even if an error row somehow
    carried nonzero tokens)."""
    msg = _ev(eid=1, account_id="default", kind="message", tokens_input=0)
    err = _ev(eid=2, account_id="user@example.com", kind="error", tokens_input=999)
    winner, loser = _pick_winner(msg, err)
    assert winner is msg
    assert loser is err
    # Order of arguments must not matter.
    winner, loser = _pick_winner(err, msg)
    assert winner is msg
    assert loser is err


def test_larger_tokens_wins_when_kind_ties():
    """Matches the observed divergent pairs (opencode: 754, opencode-free: 442)
    where the same event_id was captured at two different points in a
    still-streaming message — the more-complete (larger) copy wins,
    regardless of which account_id it happens to be tagged under."""
    smaller = _ev(eid=1, account_id="user@example.com", tokens_input=100, tokens_output=10)
    larger = _ev(eid=2, account_id="default", tokens_input=20_000, tokens_output=600)
    winner, loser = _pick_winner(smaller, larger)
    assert winner is larger
    assert loser is smaller


def test_lower_id_wins_true_tie():
    """Byte-identical duplicates (the majority case) fall through to a
    deterministic tie-break: lower id (first-ingested) wins."""
    first = _ev(eid=100, account_id="default", tokens_input=50, tokens_output=5)
    second = _ev(eid=200, account_id="user@example.com", tokens_input=50, tokens_output=5)
    winner, loser = _pick_winner(first, second)
    assert winner is first
    assert loser is second
    # Order of arguments must not matter.
    winner, loser = _pick_winner(second, first)
    assert winner is first
    assert loser is second


def test_kind_tier_outranks_token_tier():
    """A message with fewer tokens still beats an error with more tokens —
    kind is checked strictly before token magnitude."""
    msg_fewer = _ev(eid=1, account_id="default", kind="message", tokens_input=10)
    err_more = _ev(eid=2, account_id="user@example.com", kind="error", tokens_input=5000)
    winner, _ = _pick_winner(msg_fewer, err_more)
    assert winner is msg_fewer
