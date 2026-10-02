"""Which stale source rows ingest may retire, and which are the operator's.

`_can_reconcile_row` is the whole policy: a source belongs to exactly one
account, but ingest only touches placeholders (``default``, Anthropic's old
token-derived identities) and accounts nothing collects for any more — a row
an operator deliberately moved to a second real account stays put.
"""

from __future__ import annotations

from app.api.endpoints.fleet import _can_reconcile_row

HASH_ACCOUNT = "bd6d58cf00000000"


def test_a_row_already_on_the_target_account_is_left_alone():
    assert _can_reconcile_row("deepseek", "alice@example.com", "alice@example.com") is False


def test_the_default_placeholder_is_retired_for_every_provider():
    assert _can_reconcile_row("deepseek", "default", "alice@example.com", set()) is True


def test_anthropic_retires_any_other_account():
    assert _can_reconcile_row("anthropic", "some-old-id", "alice@example.com", set()) is True


def test_a_row_on_an_account_still_collecting_for_is_not_ours_to_touch():
    assert _can_reconcile_row("deepseek", "bob@example.com", "alice@example.com", set()) is False


def test_a_row_on_a_phantom_account_is_retired():
    """The account an account rename left behind: credential rows, no config,
    card or usage. Retiring them is what stops ingest re-filing a source under
    two accounts after a rekey."""
    assert (
        _can_reconcile_row("deepseek", HASH_ACCOUNT, "alice@example.com", {HASH_ACCOUNT, "default"})
        is True
    )


def test_without_a_phantom_list_only_the_placeholders_are_retired():
    assert _can_reconcile_row("deepseek", HASH_ACCOUNT, "alice@example.com") is False
