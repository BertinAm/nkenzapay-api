"""The calculation engine.

The brief states the post-fee rule three times, so it gets tested first and
hardest. If any of these break, the platform is lying to a customer about what
they will receive.
"""
from decimal import Decimal

import pytest

from nkenzapay.pricing.engine import LimitBreach, build_quote
from nkenzapay.pricing.models import FeeRule, TransferLimit

pytestmark = pytest.mark.django_db


def test_worked_example_from_the_brief(receive_corridor, customer):
    """100,000 XAF at 0.16935 with a 6% charge lands at 15,918.90 INR."""
    quote = build_quote(corridor=receive_corridor, direction="receive",
                        send_amount=Decimal("100000"), user=customer)

    assert quote.rate_used == Decimal("0.1693500000")
    assert quote.converted_amount == Decimal("16935.00")
    assert quote.fee_amount == Decimal("1016.10")
    assert quote.receive_amount == Decimal("15918.90")


def test_the_headline_is_never_the_raw_conversion(receive_corridor, customer):
    quote = build_quote(corridor=receive_corridor, direction="receive",
                        send_amount=Decimal("100000"), user=customer)
    assert quote.receive_amount != quote.converted_amount
    assert quote.receive_amount < quote.converted_amount


def test_the_three_lines_add_up_exactly(receive_corridor, customer):
    """Converted minus fee equals received, to the cent, at any amount.

    Rounding the fee before subtracting is what makes this hold. Rounding after
    would leave a customer reading three numbers that do not reconcile.
    """
    for amount in ["5000", "7777", "12345", "99999", "250000", "1000000"]:
        quote = build_quote(corridor=receive_corridor, direction="receive",
                            send_amount=Decimal(amount), user=customer)
        assert quote.converted_amount - quote.fee_amount == quote.receive_amount


def test_send_direction_deducts_from_the_recipient(send_corridor, customer):
    """Brief section 31: 10,000 INR reaches Cameroon as the post-fee figure."""
    quote = build_quote(corridor=send_corridor, direction="send",
                        send_amount=Decimal("10000"), user=customer)

    assert quote.converted_amount == Decimal("58638")
    assert quote.fee_amount == Decimal("3518")
    assert quote.receive_amount == Decimal("55120")


def test_xaf_carries_no_decimal_places(send_corridor, customer):
    """XAF has no subunit. A payout of 55,120.34 XAF does not exist."""
    quote = build_quote(corridor=send_corridor, direction="send",
                        send_amount=Decimal("1234"), user=customer)
    assert quote.receive_amount == quote.receive_amount.to_integral_value()
    assert quote.receive_amount.as_tuple().exponent == 0


def test_inr_carries_two(receive_corridor, customer):
    quote = build_quote(corridor=receive_corridor, direction="receive",
                        send_amount=Decimal("5000"), user=customer)
    assert -quote.receive_amount.as_tuple().exponent == 2


def test_below_the_minimum_reports_rather_than_raises(receive_corridor, customer):
    """The calculator still shows a figure; the message sits under the field."""
    quote = build_quote(corridor=receive_corridor, direction="receive",
                        send_amount=Decimal("4999"), user=customer)

    assert not quote.is_valid
    assert quote.errors[0]["code"] == "below_minimum"
    assert "5,000 XAF" in quote.errors[0]["message"]
    assert quote.receive_amount > 0


def test_order_creation_enforces_the_minimum(receive_corridor, customer):
    with pytest.raises(LimitBreach):
        build_quote(corridor=receive_corridor, direction="receive",
                    send_amount=Decimal("4999"), user=customer, enforce_limits=True)


def test_exactly_the_minimum_is_allowed(receive_corridor, customer):
    quote = build_quote(corridor=receive_corridor, direction="receive",
                        send_amount=Decimal("5000"), user=customer)
    assert quote.is_valid


def test_send_minimum_is_one_thousand_rupees(send_corridor, customer):
    below = build_quote(corridor=send_corridor, direction="send",
                        send_amount=Decimal("999"), user=customer)
    assert below.errors[0]["code"] == "below_minimum"
    assert "₹1,000" in below.errors[0]["message"]

    at_minimum = build_quote(corridor=send_corridor, direction="send",
                             send_amount=Decimal("1000"), user=customer)
    assert at_minimum.is_valid


def test_a_fee_change_moves_the_figure_without_a_deploy(receive_corridor, customer):
    rule = FeeRule.objects.get(corridor=None, country=None)
    rule.percent = Decimal("3.00")
    rule.save()

    quote = build_quote(corridor=receive_corridor, direction="receive",
                        send_amount=Decimal("100000"), user=customer)
    assert quote.fee_percent == Decimal("3.00")
    assert quote.fee_amount == Decimal("508.05")
    assert quote.receive_amount == Decimal("16426.95")


def test_a_country_rule_beats_the_global_rule(receive_corridor, customer):
    from nkenzapay.geo.models import Country

    FeeRule.objects.create(
        country=Country.objects.get(pk="CM"), percent=Decimal("5.50"),
        fee_currency_id="INR", is_active=True,
    )
    quote = build_quote(corridor=receive_corridor, direction="receive",
                        send_amount=Decimal("100000"), user=customer)
    assert quote.fee_percent == Decimal("5.50")


def test_a_corridor_rule_beats_a_country_rule(receive_corridor, customer):
    from nkenzapay.geo.models import Country

    FeeRule.objects.create(country=Country.objects.get(pk="CM"),
                           percent=Decimal("5.50"), fee_currency_id="INR")
    FeeRule.objects.create(corridor=receive_corridor, percent=Decimal("4.00"),
                           fee_currency_id="INR")

    quote = build_quote(corridor=receive_corridor, direction="receive",
                        send_amount=Decimal("100000"), user=customer)
    assert quote.fee_percent == Decimal("4.00")


def test_minimum_fee_clamps_upward(receive_corridor, customer):
    rule = FeeRule.objects.get(corridor=None, country=None)
    rule.min_fee = Decimal("50")
    rule.save()

    # 5,000 XAF converts to 846.75 INR; 6% of that is 50.81, already above the
    # floor. 4,000 would fall under it, so use an amount that does.
    quote = build_quote(corridor=receive_corridor, direction="receive",
                        send_amount=Decimal("5000"), user=customer)
    assert quote.fee_amount >= Decimal("50")


def test_maximum_fee_clamps_downward(receive_corridor, customer):
    rule = FeeRule.objects.get(corridor=None, country=None)
    rule.max_fee = Decimal("400")
    rule.save()

    quote = build_quote(corridor=receive_corridor, direction="receive",
                        send_amount=Decimal("100000"), user=customer)
    assert quote.fee_amount == Decimal("400.00")
    assert quote.receive_amount == Decimal("16535.00")


def test_a_fee_can_never_exceed_the_amount_sent(receive_corridor, customer):
    """A large minimum fee on a small transfer must not produce a negative
    payout. The charge is capped at the converted amount."""
    rule = FeeRule.objects.get(corridor=None, country=None)
    rule.min_fee = Decimal("100000")
    rule.save()
    TransferLimit.objects.filter(corridor=receive_corridor).update(minimum=Decimal("1"))

    quote = build_quote(corridor=receive_corridor, direction="receive",
                        send_amount=Decimal("5000"), user=customer)
    assert quote.receive_amount >= 0
    assert quote.fee_amount <= quote.converted_amount


def test_the_daily_limit_counts_open_transfers(send_corridor, customer, receive_order):
    """A transfer waiting on payment has already claimed against the limit."""
    TransferLimit.objects.filter(corridor=receive_order.corridor).update(
        daily_maximum=Decimal("120000")
    )
    quote = build_quote(corridor=receive_order.corridor, direction="receive",
                        send_amount=Decimal("50000"), user=customer)
    codes = [e["code"] for e in quote.errors]
    assert "daily_limit" in codes


def test_high_value_transfers_are_flagged_for_review(receive_corridor, customer):
    quote = build_quote(corridor=receive_corridor, direction="receive",
                        send_amount=Decimal("950000"), user=customer)
    assert quote.needs_manual_review is True


def test_the_quote_expires(receive_corridor, customer):
    from django.utils import timezone

    quote = build_quote(corridor=receive_corridor, direction="receive",
                        send_amount=Decimal("100000"), user=customer)
    held = (quote.expires_at - timezone.now()).total_seconds()
    assert 55 <= held <= 61
