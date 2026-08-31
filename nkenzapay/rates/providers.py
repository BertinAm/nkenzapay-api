"""Foreign exchange providers.

The only place in the platform that talks to an FX API. Nothing here is ever
imported by a serializer or a view that renders to the browser, and the key is
read from settings at call time so it cannot end up in a fixture or a log line.

Adding a provider means writing one class with one method. The provider row in
the database picks which one runs.
"""
from __future__ import annotations

import logging
from decimal import Decimal

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 8


class RateUnavailable(Exception):
    """The provider could not be reached or returned something unusable."""


class BaseProvider:
    slug = ""

    def fetch(self, base: str, quote: str) -> Decimal:
        raise NotImplementedError


class MockProvider(BaseProvider):
    """Development only. Returns the figures from the brief so the worked
    example in the tests matches what a developer sees on screen."""

    slug = "mock"

    TABLE = {
        ("XAF", "INR"): Decimal("0.16935"),
        ("INR", "XAF"): Decimal("5.8638"),
        ("XOF", "INR"): Decimal("0.16935"),
        ("INR", "XOF"): Decimal("5.8638"),
        ("NGN", "INR"): Decimal("0.0553"),
        ("INR", "NGN"): Decimal("18.08"),
        ("GHS", "INR"): Decimal("5.62"),
        ("INR", "GHS"): Decimal("0.178"),
    }

    def fetch(self, base, quote):
        try:
            return self.TABLE[(base.upper(), quote.upper())]
        except KeyError as exc:
            raise RateUnavailable(f"No mock rate for {base}/{quote}") from exc


class XEProvider(BaseProvider):
    """XE Currency Data. Basic auth with an account id and an API key."""

    slug = "xe"
    endpoint = "https://xecdapi.xe.com/v1/convert_from"

    def fetch(self, base, quote):
        account = settings.FX["ACCOUNT_ID"]
        key = settings.FX["API_KEY"]
        if not (account and key):
            raise RateUnavailable("XE credentials are not configured.")
        try:
            response = requests.get(
                self.endpoint,
                params={"from": base, "to": quote, "amount": 1},
                auth=(account, key),
                timeout=TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload = response.json()
            for row in payload.get("to", []):
                if row.get("quotecurrency") == quote:
                    return Decimal(str(row["mid"]))
        except (requests.RequestException, ValueError, KeyError) as exc:
            raise RateUnavailable(f"XE request failed: {exc}") from exc
        raise RateUnavailable(f"XE returned no {quote} leg for {base}.")


class OpenExchangeRatesProvider(BaseProvider):
    """Fallback provider. Quotes everything against USD, so a cross pair is two
    legs divided — which is why the raw rate is stored alongside the effective
    one, rather than being recomputed later from whatever is current."""

    slug = "openexchangerates"
    endpoint = "https://openexchangerates.org/api/latest.json"

    def fetch(self, base, quote):
        key = settings.FX["API_KEY"]
        if not key:
            raise RateUnavailable("Open Exchange Rates key is not configured.")
        try:
            response = requests.get(
                self.endpoint,
                params={"app_id": key, "symbols": f"{base},{quote}"},
                timeout=TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            rates = response.json()["rates"]
            base_per_usd = Decimal(str(rates[base.upper()]))
            quote_per_usd = Decimal(str(rates[quote.upper()]))
        except (requests.RequestException, ValueError, KeyError) as exc:
            raise RateUnavailable(f"Open Exchange Rates request failed: {exc}") from exc
        if base_per_usd == 0:
            raise RateUnavailable(f"Zero rate returned for {base}.")
        return quote_per_usd / base_per_usd


PROVIDERS = {
    MockProvider.slug: MockProvider,
    XEProvider.slug: XEProvider,
    OpenExchangeRatesProvider.slug: OpenExchangeRatesProvider,
}


def get_provider_client(slug: str) -> BaseProvider:
    try:
        return PROVIDERS[slug]()
    except KeyError as exc:
        raise RateUnavailable(f"No client is registered for provider {slug!r}.") from exc
