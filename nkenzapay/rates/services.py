"""Rate fetching and caching.

A snapshot is reused while it is younger than the provider's refresh interval.
Past that a new one is fetched, and if the provider is down the most recent
snapshot is served with its age attached so callers can decide what to do.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from django.utils import timezone

from .models import RateProvider, RateSnapshot
from .providers import RateUnavailable, get_provider_client

logger = logging.getLogger(__name__)

# How stale a snapshot may be before a quote is refused outright, even when the
# provider is unreachable. Ten minutes of drift on a live corridor is already
# generous; beyond that the desk would be pricing off yesterday.
MAX_STALE_SECONDS = 600


def active_provider() -> RateProvider:
    provider = RateProvider.objects.filter(is_active=True).first()
    if provider is None:
        raise RateUnavailable("No FX provider is switched on.")
    return provider


def get_fresh_snapshot(base, quote, provider: RateProvider | None = None) -> RateSnapshot:
    """Return a usable snapshot for base -> quote, fetching one if needed."""
    provider = provider or active_provider()
    latest = (
        RateSnapshot.objects.filter(base=base, quote=quote, provider=provider)
        .order_by("-fetched_at")
        .first()
    )
    if latest and latest.age_seconds < provider.refresh_seconds:
        return latest

    try:
        return refresh_pair(base, quote, provider)
    except RateUnavailable as exc:
        if latest and latest.age_seconds < MAX_STALE_SECONDS:
            logger.warning(
                "FX provider %s unavailable (%s); serving a snapshot %ss old.",
                provider.slug, exc, int(latest.age_seconds),
            )
            return latest
        raise


def refresh_pair(base, quote, provider: RateProvider | None = None) -> RateSnapshot:
    """Fetch one pair and write a snapshot. The markup is applied here, once."""
    provider = provider or active_provider()
    client = get_provider_client(provider.slug)
    base_code = getattr(base, "code", base)
    quote_code = getattr(quote, "code", quote)

    try:
        raw = Decimal(str(client.fetch(base_code, quote_code)))
    except RateUnavailable as exc:
        RateProvider.objects.filter(pk=provider.pk).update(last_error=str(exc))
        raise

    if raw <= 0:
        raise RateUnavailable(f"Provider returned a non-positive rate for {base_code}/{quote_code}.")

    # The markup moves the rate against the customer, which is how a spread
    # works. It is separate from the fee and is never shown as one.
    effective = (raw * provider.markup_multiplier).quantize(Decimal("0.0000000001"))

    snapshot = RateSnapshot.objects.create(
        provider=provider,
        base_id=base_code,
        quote_id=quote_code,
        raw_rate=raw.quantize(Decimal("0.0000000001")),
        effective_rate=effective,
        fetched_at=timezone.now(),
    )
    RateProvider.objects.filter(pk=provider.pk).update(
        last_success_at=snapshot.fetched_at, last_error=""
    )
    return snapshot


def refresh_all_enabled_corridors():
    """Called on a schedule. Warms every pair a customer could ask about."""
    from nkenzapay.geo.models import Corridor

    provider = active_provider()
    refreshed = []
    for corridor in Corridor.objects.filter(is_enabled=True).select_related(
        "source__currency", "target__currency"
    ):
        base = corridor.send_currency
        quote = corridor.receive_currency
        if base.code == quote.code:
            continue
        try:
            refreshed.append(refresh_pair(base, quote, provider))
        except RateUnavailable as exc:
            logger.error("Could not refresh %s/%s: %s", base.code, quote.code, exc)
    return refreshed
