"""One conversation per customer, across every transfer they have opened.

The chat used to be per order, so somebody who had sent money three times had
three chats and the desk answered whichever one the question happened to be
attached to. These cover the thread as a whole and the inbox that lists them.
"""
import pytest
from rest_framework.test import APIClient

from nkenzapay.transactions import services
from nkenzapay.transactions.models import Status

pytestmark = pytest.mark.django_db


@pytest.fixture
def api():
    return APIClient()


def second_order(customer, receive_corridor, configured_methods):
    from decimal import Decimal

    from nkenzapay.payments.models import PaymentMethod
    from nkenzapay.pricing.engine import build_quote, persist_quote

    quote = persist_quote(
        build_quote(corridor=receive_corridor, direction="receive",
                    send_amount=Decimal("50000"), user=customer),
        user=customer,
    )
    return services.create_transaction(
        user=customer, quote=quote,
        collect_method=PaymentMethod.objects.get(slug="mtn_momo"),
    )


def test_the_thread_holds_every_order_a_customer_has_opened(
    api, customer, receive_order, receive_corridor, configured_methods
):
    other = second_order(customer, receive_corridor, configured_methods)
    services.post_message(reference=receive_order.reference, sender=customer,
                          body="About my first transfer")
    services.post_message(reference=other.reference, sender=customer,
                          body="And about my second")

    api.force_authenticate(customer)
    rows = api.get("/api/v1/me/messages").json()

    bodies = [row["body"] for row in rows]
    assert "About my first transfer" in bodies
    assert "And about my second" in bodies
    # Each message still says which transfer it belongs to, which is what lets
    # one flow stay readable.
    references = {row["reference"] for row in rows if row.get("reference")}
    assert {receive_order.reference, other.reference} <= references


def test_one_customer_never_sees_anothers_thread(api, receive_order, customer, db):
    from nkenzapay.accounts.models import User

    services.post_message(reference=receive_order.reference, sender=customer,
                          body="Private to me")
    stranger = User.objects.create_user(email="nosy4@example.com",
                                        password="a-long-password-8")
    api.force_authenticate(stranger)

    assert api.get("/api/v1/me/messages").json() == []


def test_writing_lands_on_the_transfer_that_is_still_open(
    api, customer, receive_order, receive_corridor, configured_methods
):
    """A question is almost always about the transfer still running."""
    services.cancel(reference=receive_order.reference, actor=customer)
    still_open = second_order(customer, receive_corridor, configured_methods)

    api.force_authenticate(customer)
    response = api.post("/api/v1/me/messages", {"body": "Where is it?"},
                        format="json")

    assert response.status_code == 201, response.content
    assert response.json()["reference"] == still_open.reference


def test_the_desk_reads_and_answers_one_customers_thread(
    api, owner, customer, receive_order
):
    services.post_message(reference=receive_order.reference, sender=customer,
                          body="Has it arrived?")
    api.force_authenticate(owner)

    read = api.get(f"/api/v1/admin/customers/{customer.pk}/messages")
    assert read.status_code == 200, read.content
    assert read.json()["customer"]["email"] == customer.email
    assert any(m["body"] == "Has it arrived?" for m in read.json()["messages"])

    reply = api.post(f"/api/v1/admin/customers/{customer.pk}/messages",
                     {"body": "It is with us now."}, format="json")
    assert reply.status_code == 201, reply.content
    assert reply.json()["is_from_desk"] is True


def test_reading_a_thread_clears_its_unread_badge(api, owner, customer, receive_order):
    from nkenzapay.transactions.models import Message

    services.post_message(reference=receive_order.reference, sender=customer,
                          body="Unread until read")
    assert Message.objects.filter(read_at__isnull=True, is_from_desk=False).exists()

    api.force_authenticate(owner)
    api.get(f"/api/v1/admin/customers/{customer.pk}/messages")

    assert not Message.objects.filter(
        transaction__user=customer, read_at__isnull=True, is_from_desk=False
    ).exists()


def test_the_inbox_lists_people_not_transfers(
    api, owner, customer, receive_order, receive_corridor, configured_methods
):
    """Two transfers, one customer, one row."""
    other = second_order(customer, receive_corridor, configured_methods)
    services.post_message(reference=receive_order.reference, sender=customer,
                          body="One")
    services.post_message(reference=other.reference, sender=customer, body="Two")

    api.force_authenticate(owner)
    threads = api.get("/api/v1/admin/messages/inbox").json()["threads"]

    mine = [row for row in threads if row["customer_id"] == customer.pk]
    assert len(mine) == 1, "a customer with two transfers appeared twice"
    assert mine[0]["preview"] == "Two"
    assert mine[0]["unread"] >= 2


def test_the_desk_can_find_somebody_by_what_they_actually_know(
    api, owner, customer, receive_order
):
    """A name, an address or a number. A reference has to be looked up."""
    services.post_message(reference=receive_order.reference, sender=customer,
                          body="Findable")

    api.force_authenticate(owner)

    def search(term):
        rows = api.get(f"/api/v1/admin/messages/inbox?q={term}").json()["threads"]
        return [row["customer_id"] for row in rows]

    assert customer.pk in search(customer.email.split("@")[0])
    assert customer.pk in search(receive_order.reference)
    assert customer.pk not in search("nobody-by-that-name")


def test_the_desk_can_attach_a_file_to_a_customers_thread(
    api, owner, customer, receive_order
):
    """The desk sends things too: proof a payout left, a form, a screenshot."""
    api.force_authenticate(owner)

    granted = api.post(
        f"/api/v1/admin/customers/{customer.pk}/attachments/upload-url",
        {"content_type": "image/png", "size_bytes": 400, "filename": "payout.png"},
        format="json",
    )
    assert granted.status_code == 200, granted.content

    from nkenzapay.common.storage import storage

    storage().save_bytes(granted.json()["key"],
                         b"\x89PNG\r\n\x1a\n" + b"\x00" * 200, "image/png")

    committed = api.post(
        f"/api/v1/admin/customers/{customer.pk}/attachments",
        {"key": granted.json()["key"], "filename": "payout.png",
         "content_type": "image/png", "size_bytes": 208},
        format="json",
    )
    assert committed.status_code == 201, committed.content
    # Never proof: proof is what a customer sends to be verified, and a desk
    # upload marked as such would sit in the verification queue.
    assert committed.json()["is_payment_proof"] is False


def test_the_desk_can_still_answer_after_the_last_transfer_closed(
    api, owner, customer, receive_order
):
    """The customer's own chat locks when a transfer closes, and should. The
    desk answering a question afterwards should not be refused."""
    services.cancel(reference=receive_order.reference, actor=customer)
    assert receive_order.refresh_from_db() or True
    receive_order.refresh_from_db()
    assert receive_order.status == Status.CANCELLED

    api.force_authenticate(owner)
    reply = api.post(f"/api/v1/admin/customers/{customer.pk}/messages",
                     {"body": "Nothing was charged."}, format="json")

    assert reply.status_code == 201, reply.content
