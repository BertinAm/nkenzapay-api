"""Opening an order against a method the desk has finished configuring.

The fixtures fill in a number and an account name and stop there, so nothing
covered a method carrying the dial strings a real desk enters. That is the shape
production is in, and the instruction bubble renders those at order time -- the
one moment they are turned into text.
"""
import pytest
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db


@pytest.fixture
def signed_in(customer):
    client = APIClient()
    client.force_authenticate(customer)
    return client

# Exactly the shape entered on the live desk: a personal code, a business code,
# and an account name long enough to be someone's real registered name.
LIVE_MTN = {
    "number": "682109102",
    "account_name": (
        "CLEDELAND UNIVERSAL CONSULTING ASSISTING SERVICE CUCAS LTD "
        "FONGE BERTIN AMIN SHU"
    ),
    "ussd_personal": "*126*1*682109102*{amount}#",
    "ussd_business": "*126*5*1*682109102*{amount}#",
}


@pytest.fixture
def mtn_with_dial_codes(seeded):
    from nkenzapay.payments.models import PaymentInstruction

    PaymentInstruction.objects.filter(method__slug="mtn_momo").update(fields=LIVE_MTN)
    return LIVE_MTN


def open_order(client, corridor="CM"):
    quote = client.post(
        "/api/v1/rates/quote",
        {"source": "CM", "target": "IN", "direction": "receive",
         "send_amount": "100000"},
        format="json",
    ).json()
    return client.post(
        "/api/v1/transactions",
        {"quote_reference": quote["reference"], "collect_method": "mtn_momo"},
        format="json",
    )


def test_an_order_opens_against_a_method_carrying_dial_codes(
    signed_in, receive_corridor, mtn_with_dial_codes
):
    response = open_order(signed_in)

    assert response.status_code == 201, response.content


def test_a_send_order_opens_once_the_recipient_is_given(signed_in, send_corridor):
    """India to Cameroon, all the way to a created order.

    There was a test for a send order being refused without a recipient and none
    for one succeeding with a recipient, so the direction the picker now opens on
    was only ever exercised as far as its own rejection.
    """
    quote = signed_in.post(
        "/api/v1/rates/quote",
        {"source": "IN", "target": "CM", "direction": "send",
         "send_amount": "10000"},
        format="json",
    ).json()

    response = signed_in.post(
        "/api/v1/transactions",
        {
            "quote_reference": quote["reference"],
            "collect_method": "upi",
            "recipient_name": "Marie Nkenganyi",
            "recipient_number": "+237 6 70 00 00 00",
            "recipient_details": {"network": "mtn_payout"},
        },
        format="json",
    )

    assert response.status_code == 201, response.content
    assert response.json()["status"] == "awaiting_payment"


def test_a_send_order_needs_the_recipients_network(signed_in, send_corridor):
    """A Mobile Money number does not say who runs it.

    Paying an Orange number through MTN does not arrive, and the desk was left
    guessing from the prefix — a guess about somebody else's money.
    """
    quote = signed_in.post(
        "/api/v1/rates/quote",
        {"source": "IN", "target": "CM", "direction": "send",
         "send_amount": "10000"},
        format="json",
    ).json()
    order = {
        "quote_reference": quote["reference"],
        "collect_method": "upi",
        "recipient_name": "Marie Nkenganyi",
        "recipient_number": "+237 6 70 00 00 00",
    }

    missing = signed_in.post("/api/v1/transactions", order, format="json")
    assert missing.status_code == 400
    assert "recipient_details" in missing.json()["error"]["detail"]

    # A network the platform cannot pay out on is refused just as firmly as
    # none at all: it would be an order nobody could fulfil.
    invented = signed_in.post(
        "/api/v1/transactions",
        {**order, "recipient_details": {"network": "not_a_network"}},
        format="json",
    )
    assert invented.status_code == 400

    # "Another network" is allowed, but only when they say which.
    unnamed = signed_in.post(
        "/api/v1/transactions",
        {**order, "recipient_details": {"network": "other"}},
        format="json",
    )
    assert unnamed.status_code == 400

    named = signed_in.post(
        "/api/v1/transactions",
        {**order, "recipient_details": {"network": "other",
                                        "network_other": "Nexttel"}},
        format="json",
    )
    assert named.status_code == 201, named.content


def test_orange_is_a_network_money_can_be_sent_to(signed_in, send_corridor):
    """Cameroon had MTN and Orange to collect from and only MTN to pay out to,
    which was invisible until somebody had to choose one."""
    quote = signed_in.post(
        "/api/v1/rates/quote",
        {"source": "IN", "target": "CM", "direction": "send",
         "send_amount": "10000"},
        format="json",
    ).json()

    response = signed_in.post(
        "/api/v1/transactions",
        {
            "quote_reference": quote["reference"],
            "collect_method": "upi",
            "recipient_name": "Marie Nkenganyi",
            "recipient_number": "+237 6 90 00 00 00",
            "recipient_details": {"network": "orange_payout"},
        },
        format="json",
    )

    assert response.status_code == 201, response.content


def test_the_dial_code_arrives_with_the_amount_already_in_it(
    signed_in, receive_corridor, mtn_with_dial_codes
):
    """The customer dials what they are shown rather than editing it."""
    reference = open_order(signed_in).json()["reference"]

    messages = signed_in.get(f"/api/v1/transactions/{reference}/messages").json()
    rows = [
        row
        for message in messages
        for row in (message.get("payload") or {}).get("rows", [])
    ]
    dialled = [row["value"] for row in rows if row["value"].startswith("*126")]

    assert "*126*1*682109102*100000#" in dialled
    assert "*126*5*1*682109102*100000#" in dialled
    # XAF has no minor units, so no decimal point reaches the handset.
    assert not any("." in code for code in dialled)
