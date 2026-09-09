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
    label = ""
    #: Keys in settings.FX this provider cannot work without. Read by the
    #: deploy check, so a provider switched on without its credentials is
    #: caught at deploy rather than by the first customer to ask for a price.
    needs: tuple[str, ...] = ()

    def fetch(self, base: str, quote: str) -> Decimal:
        raise NotImplementedError


class MockProvider(BaseProvider):
    """Development only. Returns the figures from the brief so the worked
    example in the tests matches what a developer sees on screen."""

    slug = "mock"
    label = "Development rates"

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
    label = "XE Currency Data"
    needs = ("API_KEY", "ACCOUNT_ID")
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
    label = "Open Exchange Rates"
    needs = ("API_KEY",)
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


class ExchangeRateApiProvider(BaseProvider):
    """ExchangeRate-API's open endpoint. No key, no account, no card.

    The one free source checked that actually quotes XAF. Frankfurter is free
    and keyless too and covers far more currencies, but not the CFA franc:
    XAF/INR comes back "not found", which is no use to a platform whose whole
    business is Cameroon.

    Quoted against USD like Open Exchange Rates, so a cross pair is two legs
    divided. Updated once a day, which is worth knowing rather than working
    around: refresh_seconds on the provider row should say hours, not seconds,
    and a quote held for sixty seconds is still honest because the figure it
    holds is the figure the source published.

    Free as in no invoice, not as in no obligations. Their terms ask for
    attribution on the free tier; read them before this prices anything real.
    """

    slug = "exchangerate_api"
    label = "ExchangeRate-API (free)"
    endpoint = "https://open.er-api.com/v6/latest/{base}"

    def fetch(self, base, quote):
        base = base.upper()
        quote = quote.upper()
        try:
            response = requests.get(
                self.endpoint.format(base=base), timeout=TIMEOUT_SECONDS
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise RateUnavailable(f"ExchangeRate-API request failed: {exc}") from exc

        # It answers 200 with result: error rather than an HTTP status, so the
        # body has to be read before the rates are trusted.
        if payload.get("result") != "success":
            raise RateUnavailable(
                f"ExchangeRate-API refused {base}: "
                f"{payload.get('error-type', 'no reason given')}"
            )

        rate = (payload.get("rates") or {}).get(quote)
        if rate in (None, 0):
            raise RateUnavailable(f"ExchangeRate-API has no {quote} rate for {base}.")
        return Decimal(str(rate))


PROVIDERS = {
    MockProvider.slug: MockProvider,
    XEProvider.slug: XEProvider,
    OpenExchangeRatesProvider.slug: OpenExchangeRatesProvider,
    ExchangeRateApiProvider.slug: ExchangeRateApiProvider,
}


# settings.FX key -> the name to put in .env. They differ, and a check that
# tells somebody to set FX_ACCOUNT_ID when the variable is FX_API_ACCOUNT_ID
# sends them to edit a file and change nothing.
ENV_NAMES = {
    "API_KEY": "FX_API_KEY",
    "ACCOUNT_ID": "FX_API_ACCOUNT_ID",
}


def credentials_missing(slug: str) -> tuple[str, ...]:
    """The .env names this provider needs and does not have."""
    provider = PROVIDERS.get(slug)
    if provider is None:
        return ()
    return tuple(
        ENV_NAMES.get(name, name)
        for name in provider.needs
        if not settings.FX.get(name)
    )


def get_provider_client(slug: str) -> BaseProvider:
    try:
        return PROVIDERS[slug]()
    except KeyError as exc:
        raise RateUnavailable(f"No client is registered for provider {slug!r}.") from exc
