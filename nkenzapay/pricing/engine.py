"""The calculation engine.

One function decides every figure a customer ever sees. The quote endpoint and
order creation both call it, so a price cannot drift between the screen that
promised it and the order that recorded it.

The rule the brief states three times, and the one this file exists to enforce:

    the amount shown as "you receive" is the conversion minus the charge

The raw conversion is a working line. It is never the promise.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.db.models import Q, Sum
from django.utils import timezone

from nkenzapay.common.money import quantize
from nkenzapay.rates.models import Quote
from nkenzapay.rates.services import active_provider, get_fresh_snapshot

from .models import FeeRule, TransferLimit

RECEIVE = "receive"
SEND = "send"


@dataclass
class QuoteResult:
    """What the engine produces. Deliberately not a model instance: a quote is
    only persisted once someone commits to it."""

    corridor: object
    direction: str
    snapshot: object
    send_currency: object
    receive_currency: object
    send_amount: Decimal
    converted_amount: Decimal
    fee_percent: Decimal
    fee_amount: Decimal
    receive_amount: Decimal
    rate_used: Decimal
    expires_at: object
    limit: object | None = None
    needs_manual_review: bool = False
    errors: list = field(default_factory=list)

    @property
    def is_valid(self):
        return not self.errors


class LimitBreach(Exception):
    """Raised only when a caller asks the engine to enforce rather than report.

    The calculator reports, so a customer sees the message under the field
    instead of a failed request. Order creation enforces."""

    def __init__(self, errors):
        self.errors = errors
        super().__init__("; ".join(e["message"] for e in errors))


def resolve_fee_rule(corridor, direction, at=None):
    """Corridor beats country beats global; a rule scoped to one direction
    beats one that covers both."""
    at = at or timezone.now()
    candidates = (
        FeeRule.objects.filter(is_active=True, valid_from__lte=at)
        .filter(Q(valid_to__isnull=True) | Q(valid_to__gt=at))
        .filter(Q(direction="") | Q(direction=direction))
        .filter(
            Q(corridor=corridor)
            | Q(corridor__isnull=True, country=corridor.source)
            | Q(corridor__isnull=True, country=corridor.target)
            | Q(corridor__isnull=True, country__isnull=True)
        )
        .select_related("fee_currency")
    )
    ranked = sorted(candidates, key=lambda r: (r.specificity, r.valid_from), reverse=True)
    if not ranked:
        raise LookupError(
            "No fee rule covers this corridor. Seed a global rule before quoting."
        )
    return ranked[0]


def resolve_limit(corridor, direction):
    return TransferLimit.objects.filter(corridor=corridor, direction=direction).first()


def convert_between(amount: Decimal, from_currency, to_currency, rate: Decimal) -> Decimal:
    """Move a fee bound onto the receive side.

    A minimum fee configured in INR has to be comparable with a fee computed in
    INR. When the bound is already in the target currency this is a no-op; when
    it is in the source currency it goes through the same rate the quote used,
    never a second lookup, so the arithmetic on the receipt stays closed.
    """
    from_code = getattr(from_currency, "code", from_currency)
    to_code = getattr(to_currency, "code", to_currency)
    if from_code == to_code:
        return quantize(amount, to_currency)
    return quantize(Decimal(amount) * rate, to_currency)


def build_quote(
    *,
    corridor,
    direction,
    send_amount,
    user=None,
    enforce_limits=False,
    at=None,
):
    """Price one transfer.

    Order of operations matters and is fixed: convert, charge, subtract. The fee
    is rounded before it is subtracted so the three lines a customer reads —
    converted, fee, received — add up exactly on screen and on the receipt.
    """
    at = at or timezone.now()
    send_amount = Decimal(str(send_amount))
    send_currency = corridor.send_currency
    receive_currency = corridor.receive_currency

    snapshot = get_fresh_snapshot(send_currency, receive_currency)
    rate = snapshot.effective_rate

    converted = quantize(send_amount * rate, receive_currency)

    rule = resolve_fee_rule(corridor, direction, at=at)
    fee = quantize(converted * rule.percent / Decimal(100), receive_currency)

    # Clamps are applied after the percentage and in the receive currency,
    # because that is the side the money comes off.
    if rule.min_fee is not None:
        floor = convert_between(rule.min_fee, rule.fee_currency, receive_currency, rate)
        fee = max(fee, floor)
    if rule.max_fee is not None:
        ceiling = convert_between(rule.max_fee, rule.fee_currency, receive_currency, rate)
        fee = min(fee, ceiling)

    # A charge can never exceed what is being sent. Small transfers under a
    # minimum-fee rule would otherwise produce a negative payout.
    fee = min(fee, converted)

    receive = quantize(converted - fee, receive_currency)

    limit = resolve_limit(corridor, direction)
    errors = validate_against_limits(
        send_amount=send_amount,
        send_currency=send_currency,
        limit=limit,
        user=user,
        at=at,
    )
    if errors and enforce_limits:
        raise LimitBreach(errors)

    needs_review = bool(
        limit
        and limit.manual_review_above is not None
        and send_amount > limit.manual_review_above
    )

    provider = snapshot.provider or active_provider()
    return QuoteResult(
        corridor=corridor,
        direction=direction,
        snapshot=snapshot,
        send_currency=send_currency,
        receive_currency=receive_currency,
        send_amount=quantize(send_amount, send_currency),
        converted_amount=converted,
        fee_percent=rule.percent,
        fee_amount=fee,
        receive_amount=receive,
        rate_used=rate,
        expires_at=at + timedelta(seconds=provider.hold_seconds),
        limit=limit,
        needs_manual_review=needs_review,
        errors=errors,
    )


def format_limit(amount: Decimal, currency_code: str) -> str:
    """A limit is a round figure, and reads like one.

    "Minimum transfer amount is 1,000.00 INR" is what a database column says.
    The brief asks for "₹1,000", so a whole-number bound loses its empty
    decimals before it reaches the customer. A bound that genuinely has a
    fraction keeps it.
    """
    from nkenzapay.common.money import display_amount, group_indian, group_western

    amount = Decimal(amount)
    if amount != amount.to_integral_value():
        return display_amount(amount, currency_code)

    whole = f"{amount.to_integral_value():.0f}"
    code = currency_code.upper()
    grouped = group_indian(whole) if code == "INR" else group_western(whole)
    return f"₹{grouped}" if code == "INR" else f"{grouped} {code}"


def validate_against_limits(*, send_amount, send_currency, limit, user=None, at=None):
    """Report every breach rather than the first one, so the form can show all
    of them at once instead of drip-feeding the customer."""
    at = at or timezone.now()
    errors = []
    if limit is None:
        return errors

    code = send_currency.code

    if send_amount < limit.minimum:
        errors.append({
            "code": "below_minimum",
            "field": "send_amount",
            "message": f"Minimum transfer amount is {format_limit(limit.minimum, code)}.",
        })

    if limit.maximum is not None and send_amount > limit.maximum:
        errors.append({
            "code": "above_maximum",
            "field": "send_amount",
            "message": f"Maximum transfer amount is {format_limit(limit.maximum, code)}.",
        })

    if user is not None and user.is_authenticated:
        if limit.daily_maximum is not None:
            spent = _volume_since(user, at - timedelta(days=1), send_currency)
            if spent + send_amount > limit.daily_maximum:
                remaining = max(Decimal(0), limit.daily_maximum - spent)
                errors.append({
                    "code": "daily_limit",
                    "field": "send_amount",
                    "message": (
                        f"This would pass your daily limit. "
                        f"You have {format_limit(remaining, code)} left today."
                    ),
                })
        if limit.monthly_maximum is not None:
            spent = _volume_since(user, at - timedelta(days=30), send_currency)
            if spent + send_amount > limit.monthly_maximum:
                remaining = max(Decimal(0), limit.monthly_maximum - spent)
                errors.append({
                    "code": "monthly_limit",
                    "field": "send_amount",
                    "message": (
                        f"This would pass your monthly limit. "
                        f"You have {format_limit(remaining, code)} left this month."
                    ),
                })

    return errors


def _volume_since(user, since, currency):
    """Everything the customer has already committed in this currency.

    Cancelled and rejected transfers are excluded; open ones are not, because a
    transfer that is waiting on payment has still been claimed against a limit.
    """
    from nkenzapay.transactions.models import Status, Transaction

    total = (
        Transaction.objects.filter(
            user=user,
            send_currency=currency,
            created_at__gte=since,
        )
        .exclude(status__in=[Status.CANCELLED, Status.REJECTED])
        .aggregate(total=Sum("send_amount"))["total"]
    )
    return total or Decimal(0)


def persist_quote(result: QuoteResult, user=None) -> Quote:
    """Write a priced result down so an order can point at it."""
    return Quote.objects.create(
        reference=generate_quote_reference(),
        user=user if (user and user.is_authenticated) else None,
        corridor=result.corridor,
        direction=result.direction,
        snapshot=result.snapshot,
        send_currency=result.send_currency,
        receive_currency=result.receive_currency,
        send_amount=result.send_amount,
        converted_amount=result.converted_amount,
        fee_percent=result.fee_percent,
        fee_amount=result.fee_amount,
        receive_amount=result.receive_amount,
        rate_used=result.rate_used,
        expires_at=result.expires_at,
    )


def generate_quote_reference():
    return "Q" + secrets.token_hex(10).upper()
