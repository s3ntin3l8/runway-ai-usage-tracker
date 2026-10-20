"""The billing model is read off the shape of the cards an account publishes."""

from app.services.billing_type import (
    PAY_AS_YOU_GO,
    SUBSCRIPTION,
    UNKNOWN,
    card_billing_type,
    card_pct,
    detect_billing_type,
)


def test_a_percentage_of_a_limit_is_a_subscription():
    assert card_pct({"used_value": 25.0, "limit_value": 100.0}) == 25.0
    assert card_billing_type({"used_value": 25.0, "limit_value": 100.0}) == SUBSCRIPTION
    assert detect_billing_type([{"used_value": 25.0, "limit_value": 100.0}]) == SUBSCRIPTION


def test_an_explicit_percentage_wins_over_the_derived_one():
    assert card_pct({"pct_used": 42.0, "used_value": 1.0, "limit_value": 100.0}) == 42.0


def test_a_currency_balance_is_pay_as_you_go():
    balance = {"unit_type": "currency", "currency": "USD", "remaining": "$12.34"}
    assert card_billing_type(balance) == PAY_AS_YOU_GO
    assert detect_billing_type([balance]) == PAY_AS_YOU_GO


def test_a_token_count_says_nothing_about_the_billing_model():
    tokens = {"unit_type": "token", "used_value": 1000}
    assert card_billing_type(tokens) == UNKNOWN
    assert detect_billing_type([tokens]) == UNKNOWN


def test_a_quota_card_wins_over_a_spend_card():
    """An account publishing both is a plan with prepaid usage on top."""
    cards = [
        {"unit_type": "currency", "currency": "USD"},
        {"used_value": 10.0, "limit_value": 100.0},
    ]
    assert detect_billing_type(cards) == SUBSCRIPTION


def test_a_limit_of_zero_never_yields_a_percentage():
    assert card_pct({"used_value": 5.0, "limit_value": 0}) is None
    assert card_billing_type({"used_value": 5.0, "limit_value": 0}) == UNKNOWN


def test_a_card_without_numbers_is_neither_quota_nor_spend():
    assert card_pct({}) is None
    assert detect_billing_type([{}]) == UNKNOWN
    assert detect_billing_type([]) == UNKNOWN
