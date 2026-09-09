"""The free provider, against the shape the real endpoint returns.

No network here. The payloads are copies of what open.er-api.com actually
answered, so the parsing is tested against the real thing rather than against an
idea of it.
"""
from decimal import Decimal
from unittest import mock

import pytest
import requests

from nkenzapay.rates.providers import (
    ExchangeRateApiProvider,
    RateUnavailable,
    credentials_missing,
)

# Trimmed from a live response on 2026-09-09.
LIVE = {
    "result": "success",
    "provider": "https://www.exchangerate-api.com",
    "time_last_update_utc": "Wed, 09 Sep 2026 00:02:31 +0000",
    "base_code": "XAF",
    "rates": {"XAF": 1, "INR": 0.168075, "USD": 0.001772},
}


def answer(payload, status=200):
    response = mock.Mock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    response.status_code = status
    return response


def test_it_reads_the_rate_for_the_pair_asked_for():
    with mock.patch("requests.get", return_value=answer(LIVE)) as get:
        rate = ExchangeRateApiProvider().fetch("XAF", "INR")

    assert rate == Decimal("0.168075")
    # The base goes in the path, not a query string.
    assert get.call_args.args[0].endswith("/XAF")


def test_a_body_that_says_error_is_not_treated_as_a_rate():
    """It answers 200 with result: error, so the status code proves nothing."""
    payload = {"result": "error", "error-type": "unsupported-code"}

    with mock.patch("requests.get", return_value=answer(payload)):
        with pytest.raises(RateUnavailable) as exc:
            ExchangeRateApiProvider().fetch("XAF", "ZZZ")

    assert "unsupported-code" in str(exc.value)


def test_a_missing_or_zero_rate_is_refused_rather_than_dividing_by_it():
    for rates in ({"XAF": 1}, {"XAF": 1, "INR": 0}):
        payload = {**LIVE, "rates": rates}
        with mock.patch("requests.get", return_value=answer(payload)):
            with pytest.raises(RateUnavailable):
                ExchangeRateApiProvider().fetch("XAF", "INR")


def test_a_network_failure_is_reported_as_the_rate_being_unavailable():
    with mock.patch("requests.get", side_effect=requests.Timeout("too slow")):
        with pytest.raises(RateUnavailable):
            ExchangeRateApiProvider().fetch("XAF", "INR")


def test_the_free_provider_asks_for_no_credentials(settings):
    settings.FX = {"API_KEY": "", "ACCOUNT_ID": ""}

    assert credentials_missing("exchangerate_api") == ()
    assert credentials_missing("xe") == ("FX_API_KEY", "FX_API_ACCOUNT_ID")
    assert credentials_missing("openexchangerates") == ("FX_API_KEY",)
    # An unknown slug asks for nothing rather than raising: the deploy check
    # calls this and must not itself be the thing that breaks.
    assert credentials_missing("nothing-registered-here") == ()
