"""The transaction lifecycle.

Every legal move, and every illegal one refused. A status machine that lets a
transfer skip verification is a status machine that pays out on a screenshot
nobody looked at.
"""
from decimal import Decimal

import pytest

from nkenzapay.common.exceptions import ChatLocked, DomainError, IllegalTransition, QuoteExpired
from nkenzapay.transactions import services
from nkenzapay.transactions.models import MessageKind, Status, Transaction

pytestmark = pytest.mark.django_db


def make_quote(corridor, user, amount="100000", direction="receive"):
    from nkenzapay.pricing.engine import build_quote, persist_quote

    result = build_quote(corridor=corridor, direction=direction,
                         send_amount=Decimal(amount), user=user)
    return persist_quote(result, user=user)


def attach_proof(txn, user):
    return services.attach_file(
        reference=txn.reference, user=user, storage_key="test/proof.png",
        original_name="momo-receipt.png", content_type="image/png",
        size_bytes=4200, is_payment_proof=True,
    )


# --- creation ------------------------------------------------------------


def test_creation_freezes_the_quote(receive_order):
    assert receive_order.rate_used == Decimal("0.1693500000")
    assert receive_order.fee_percent == Decimal("6.00")
    assert receive_order.receive_amount == Decimal("15918.90")


def test_a_later_fee_change_does_not_move_an_existing_order(receive_order):
    from nkenzapay.pricing.models import FeeRule

    FeeRule.objects.filter(country=None, corridor=None).update(percent=Decimal("2.00"))
    receive_order.refresh_from_db()
    assert receive_order.fee_percent == Decimal("6.00")
    assert receive_order.receive_amount == Decimal("15918.90")


def test_the_reference_follows_the_brief_format(receive_order):
    import re

    assert re.fullmatch(r"NKP-\d{8}-\d{5}", receive_order.reference)


def test_references_do_not_collide(receive_corridor, customer):
    from nkenzapay.payments.models import PaymentMethod

    method = PaymentMethod.objects.get(slug="mtn_momo")
    references = set()
    for _ in range(5):
        quote = make_quote(receive_corridor, customer)
        txn = services.create_transaction(user=customer, quote=quote,
                                          collect_method=method)
        references.add(txn.reference)
    assert len(references) == 5


def test_the_two_automatic_messages_are_seeded(receive_order):
    kinds = list(receive_order.messages.values_list("kind", flat=True))
    assert kinds[0] == MessageKind.SYSTEM_NOTICE
    assert kinds[1] == MessageKind.SYSTEM_INSTRUCTIONS

    notice = receive_order.messages.first()
    assert "may result in legal action" in notice.body


def test_the_instructions_carry_the_admin_configured_details(receive_order):
    instructions = receive_order.messages.filter(
        kind=MessageKind.SYSTEM_INSTRUCTIONS
    ).first()
    rows = {r["label"]: r["value"] for r in instructions.payload["rows"]}
    assert rows["Number"] == "6 00 000 000"
    assert rows["Account name"] == "NkenzaPay"
    assert rows["Reference"].startswith("NKP-")


def test_a_new_order_is_awaiting_payment(receive_order):
    assert receive_order.status == Status.AWAITING_PAYMENT


def test_an_expired_quote_is_refused(receive_corridor, customer):
    from datetime import timedelta

    from django.utils import timezone
    from nkenzapay.payments.models import PaymentMethod

    quote = make_quote(receive_corridor, customer)
    quote.expires_at = timezone.now() - timedelta(seconds=1)
    quote.save()

    with pytest.raises(QuoteExpired):
        services.create_transaction(user=customer, quote=quote,
                                    collect_method=PaymentMethod.objects.get(slug="mtn_momo"))


def test_a_quote_cannot_be_spent_twice(receive_corridor, customer):
    from nkenzapay.payments.models import PaymentMethod

    method = PaymentMethod.objects.get(slug="mtn_momo")
    quote = make_quote(receive_corridor, customer)
    services.create_transaction(user=customer, quote=quote, collect_method=method)

    with pytest.raises(DomainError) as exc:
        services.create_transaction(user=customer, quote=quote, collect_method=method)
    assert exc.value.code == "quote_used"


def test_a_suspended_account_cannot_open_an_order(receive_corridor, customer):
    from nkenzapay.payments.models import PaymentMethod

    quote = make_quote(receive_corridor, customer)
    customer.is_suspended = True
    customer.save()

    with pytest.raises(DomainError) as exc:
        services.create_transaction(user=customer, quote=quote,
                                    collect_method=PaymentMethod.objects.get(slug="mtn_momo"))
    assert exc.value.code == "account_suspended"


# --- the happy path -------------------------------------------------------


def test_a_full_transfer_end_to_end(receive_order, customer, desk):
    attach_proof(receive_order, customer)
    services.customer_paid(reference=receive_order.reference, user=customer)
    receive_order.refresh_from_db()
    assert receive_order.status == Status.PAYMENT_VERIFICATION

    services.verify_payment(reference=receive_order.reference, admin_user=desk)
    receive_order.refresh_from_db()
    assert receive_order.status == Status.PAYOUT_PROCESSING
    assert receive_order.verified_by_id == desk.id

    services.mark_payout_sent(reference=receive_order.reference, admin_user=desk)
    receive_order.refresh_from_db()
    assert receive_order.status == Status.AWAITING_CONFIRMATION

    txn, receipt = services.confirm_received(reference=receive_order.reference,
                                             user=customer)
    assert txn.status == Status.COMPLETED
    assert receipt.snapshot["amount_received"] == "₹15,918.90"
    assert receipt.snapshot["fee_percent"] == "6.00"


def test_completion_locks_the_chat(receive_order, customer, desk):
    attach_proof(receive_order, customer)
    services.customer_paid(reference=receive_order.reference, user=customer)
    services.verify_payment(reference=receive_order.reference, admin_user=desk)
    services.mark_payout_sent(reference=receive_order.reference, admin_user=desk)
    services.confirm_received(reference=receive_order.reference, user=customer)

    with pytest.raises(ChatLocked):
        services.post_message(reference=receive_order.reference, sender=customer,
                              body="hello")


def test_the_chat_survives_completion(receive_order, customer, desk):
    attach_proof(receive_order, customer)
    services.customer_paid(reference=receive_order.reference, user=customer)
    services.verify_payment(reference=receive_order.reference, admin_user=desk)
    services.mark_payout_sent(reference=receive_order.reference, admin_user=desk)
    services.confirm_received(reference=receive_order.reference, user=customer)

    receive_order.refresh_from_db()
    assert receive_order.messages.count() > 4
    assert receive_order.chat_is_locked is True


def test_every_step_is_written_to_history(receive_order, customer, desk):
    attach_proof(receive_order, customer)
    services.customer_paid(reference=receive_order.reference, user=customer)
    services.verify_payment(reference=receive_order.reference, admin_user=desk)
    services.mark_payout_sent(reference=receive_order.reference, admin_user=desk)
    services.confirm_received(reference=receive_order.reference, user=customer)

    moves = list(receive_order.history.values_list("to_status", flat=True))
    assert moves == [
        Status.ORDER_CREATED, Status.AWAITING_PAYMENT, Status.PROOF_SUBMITTED,
        Status.PAYMENT_VERIFICATION, Status.PAYMENT_CONFIRMED,
        Status.PAYOUT_PROCESSING, Status.PAYOUT_SENT,
        Status.AWAITING_CONFIRMATION, Status.COMPLETED,
    ]


def test_every_desk_action_is_audited(receive_order, customer, desk):
    from nkenzapay.audit.models import AuditEntry

    attach_proof(receive_order, customer)
    services.customer_paid(reference=receive_order.reference, user=customer)
    services.verify_payment(reference=receive_order.reference, admin_user=desk)
    services.mark_payout_sent(reference=receive_order.reference, admin_user=desk)

    actions = set(AuditEntry.objects.values_list("action", flat=True))
    assert "transaction.created" in actions
    assert "transaction.verified" in actions
    assert "transaction.payout_sent" in actions


# --- refusals -------------------------------------------------------------


def test_i_have_paid_needs_proof_first(receive_order, customer):
    with pytest.raises(DomainError) as exc:
        services.customer_paid(reference=receive_order.reference, user=customer)
    assert exc.value.code == "proof_required"


def test_a_transfer_cannot_skip_verification(receive_order, desk):
    with pytest.raises(IllegalTransition):
        services.mark_payout_sent(reference=receive_order.reference, admin_user=desk)


def test_support_cannot_verify_a_payment(receive_order, customer, support_only):
    attach_proof(receive_order, customer)
    services.customer_paid(reference=receive_order.reference, user=customer)

    with pytest.raises(DomainError) as exc:
        services.verify_payment(reference=receive_order.reference,
                                admin_user=support_only)
    assert exc.value.code == "forbidden"


def test_an_admin_without_2fa_cannot_verify(receive_order, customer, db):
    from django.utils import timezone

    from nkenzapay.accounts.models import AdminRole, AdminUser, User

    user = User.objects.create_user(email="new@nkenzapay.com", password="a-long-password-9")
    AdminUser.objects.create(user=user, role=AdminRole.PAYMENTS, totp_confirmed_at=None)

    attach_proof(receive_order, customer)
    services.customer_paid(reference=receive_order.reference, user=customer)

    with pytest.raises(DomainError) as exc:
        services.verify_payment(reference=receive_order.reference, admin_user=user)
    assert exc.value.code == "totp_required"


def test_rejection_needs_a_reason(receive_order, customer, desk):
    attach_proof(receive_order, customer)
    services.customer_paid(reference=receive_order.reference, user=customer)

    with pytest.raises(DomainError) as exc:
        services.reject_payment(reference=receive_order.reference,
                                admin_user=desk, reason="  ")
    assert exc.value.code == "reason_required"


def test_rejection_posts_the_reason_into_the_chat(receive_order, customer, desk):
    attach_proof(receive_order, customer)
    services.customer_paid(reference=receive_order.reference, user=customer)
    services.reject_payment(reference=receive_order.reference, admin_user=desk,
                            reason="The screenshot shows a different account.")

    receive_order.refresh_from_db()
    assert receive_order.status == Status.REJECTED
    last = receive_order.messages.last()
    assert "different account" in last.body


def test_a_customer_cannot_act_on_another_customers_transfer(receive_order, db):
    from nkenzapay.accounts.models import User

    stranger = User.objects.create_user(email="other@example.com",
                                        password="a-long-password-4")
    with pytest.raises(DomainError) as exc:
        services.customer_paid(reference=receive_order.reference, user=stranger)
    assert exc.value.code == "not_yours"


def test_cancellation_is_allowed_before_payment(receive_order, customer):
    txn = services.cancel(reference=receive_order.reference, actor=customer,
                          reason="Changed my mind")
    assert txn.status == Status.CANCELLED


def test_cancellation_is_refused_after_verification(receive_order, customer, desk):
    attach_proof(receive_order, customer)
    services.customer_paid(reference=receive_order.reference, user=customer)
    services.verify_payment(reference=receive_order.reference, admin_user=desk)

    with pytest.raises(IllegalTransition):
        services.cancel(reference=receive_order.reference, actor=customer)


# --- disputes --------------------------------------------------------------


def test_not_received_opens_a_dispute(receive_order, customer, desk):
    attach_proof(receive_order, customer)
    services.customer_paid(reference=receive_order.reference, user=customer)
    services.verify_payment(reference=receive_order.reference, admin_user=desk)
    services.mark_payout_sent(reference=receive_order.reference, admin_user=desk)

    dispute = services.open_dispute(reference=receive_order.reference, user=customer,
                                    reason_code="not_received",
                                    detail="Nothing has arrived.")
    receive_order.refresh_from_db()
    assert receive_order.status == Status.DISPUTED
    assert dispute.reason_display == "I have not received my money"


def test_a_dispute_before_payout_does_not_change_the_status(receive_order, customer):
    services.open_dispute(reference=receive_order.reference, user=customer,
                          reason_code="wrong_details")
    receive_order.refresh_from_db()
    assert receive_order.status == Status.AWAITING_PAYMENT
    assert receive_order.disputes.count() == 1


# --- receipts --------------------------------------------------------------


def test_the_receipt_records_the_post_fee_figure(receive_order, customer, desk):
    attach_proof(receive_order, customer)
    services.customer_paid(reference=receive_order.reference, user=customer)
    services.verify_payment(reference=receive_order.reference, admin_user=desk)
    services.mark_payout_sent(reference=receive_order.reference, admin_user=desk)
    _txn, receipt = services.confirm_received(reference=receive_order.reference,
                                              user=customer)

    assert receipt.snapshot["amount_sent"] == "100,000 XAF"
    assert receipt.snapshot["converted"] == "₹16,935.00"
    assert receipt.snapshot["amount_received"] == "₹15,918.90"


def test_the_receipt_pdf_renders(receive_order, customer, desk):
    from nkenzapay.transactions.receipts import render_pdf

    attach_proof(receive_order, customer)
    services.customer_paid(reference=receive_order.reference, user=customer)
    services.verify_payment(reference=receive_order.reference, admin_user=desk)
    services.mark_payout_sent(reference=receive_order.reference, admin_user=desk)
    _txn, receipt = services.confirm_received(reference=receive_order.reference,
                                              user=customer)

    pdf = render_pdf(receipt).getvalue()
    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 1000
