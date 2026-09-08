"""Idempotency against a request whose body has already been read.

The whole suite authenticates with force_authenticate, which sets the user
directly and never runs session authentication. Production does run it, and its
CSRF check reads request.POST looking for a token, which on a DRF request parses
the body and marks the stream read. Every idempotent POST from a real browser
then died on request.body with RawPostDataException, while every test passed.

So these do it the way a browser does: a session, a CSRF token, and the checks
actually enforced.
"""
import json

import pytest
from rest_framework.parsers import JSONParser
from rest_framework.request import Request
from rest_framework.test import APIClient, APIRequestFactory

from nkenzapay.security.idempotency import fingerprint

pytestmark = pytest.mark.django_db


def test_the_fingerprint_survives_the_stream_being_read():
    """The unit form of the failure, without needing a whole login."""
    factory = APIRequestFactory()
    raw = factory.post("/api/v1/transactions", data={"a": 1}, format="json")
    request = Request(raw, parsers=[JSONParser()])

    # What session authentication's CSRF check does before the view is reached.
    _ = request.POST

    assert fingerprint(request)


def test_the_same_payload_hashes_the_same_whatever_the_key_order():
    factory = APIRequestFactory()

    def hashed(body):
        request = Request(
            factory.post("/x", data=json.loads(body), format="json"),
            parsers=[JSONParser()],
        )
        return fingerprint(request)

    assert hashed('{"a": 1, "b": 2}') == hashed('{"b": 2, "a": 1}')
    assert hashed('{"a": 1}') != hashed('{"a": 2}')


def test_an_order_opens_over_a_real_session(customer, receive_corridor, seeded):
    """End to end the way the browser does it, CSRF enforced.

    force_authenticate would skip the authentication class whose side effect is
    the entire bug, so this signs in through the API and carries the cookie.
    """
    customer.set_password("a-long-enough-password")
    customer.save()

    client = APIClient(enforce_csrf_checks=True)
    signed_in = client.post(
        "/api/v1/auth/login",
        {"email": customer.email, "password": "a-long-enough-password"},
        format="json",
    )
    assert signed_in.status_code == 200, signed_in.content

    token = client.cookies["csrftoken"].value
    quote = client.post(
        "/api/v1/rates/quote",
        {"source": "CM", "target": "IN", "direction": "receive",
         "send_amount": "100000"},
        format="json",
        HTTP_X_CSRFTOKEN=token,
    ).json()

    response = client.post(
        "/api/v1/transactions",
        {"quote_reference": quote["reference"], "collect_method": "mtn_momo"},
        format="json",
        HTTP_X_CSRFTOKEN=token,
        HTTP_IDEMPOTENCY_KEY="37289513-cc5e-4334-9a8e-01783e2cbba8",
    )

    assert response.status_code == 201, response.content


def test_the_same_key_replays_rather_than_opening_a_second_order(
    customer, receive_corridor, seeded
):
    """The reason the decorator exists, over the path that was broken."""
    from nkenzapay.transactions.models import Transaction

    customer.set_password("a-long-enough-password")
    customer.save()

    client = APIClient(enforce_csrf_checks=True)
    client.post(
        "/api/v1/auth/login",
        {"email": customer.email, "password": "a-long-enough-password"},
        format="json",
    )
    token = client.cookies["csrftoken"].value
    quote = client.post(
        "/api/v1/rates/quote",
        {"source": "CM", "target": "IN", "direction": "receive",
         "send_amount": "100000"},
        format="json",
        HTTP_X_CSRFTOKEN=token,
    ).json()

    body = {"quote_reference": quote["reference"], "collect_method": "mtn_momo"}
    key = "a-key-a-shaky-connection-would-retry-with"

    first = client.post("/api/v1/transactions", body, format="json",
                        HTTP_X_CSRFTOKEN=token, HTTP_IDEMPOTENCY_KEY=key)
    second = client.post("/api/v1/transactions", body, format="json",
                         HTTP_X_CSRFTOKEN=token, HTTP_IDEMPOTENCY_KEY=key)

    assert first.status_code == 201, first.content
    assert second.status_code == 201, second.content
    assert second["Idempotent-Replay"] == "true"
    assert first.json()["reference"] == second.json()["reference"]
    assert Transaction.objects.count() == 1
