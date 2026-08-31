"""The transaction lifecycle.

Nothing outside this module assigns `transaction.status`. Every move is a named
function that validates the current state, writes history, writes audit, raises
notifications, publishes on the realtime channel, and — where the move closes
the transfer — generates the receipt and locks the chat.

That discipline is what makes the audit trail trustworthy. A status set by hand
somewhere else would leave a transfer whose history does not explain it.
"""
from __future__ import annotations

from django.db import transaction as db_transaction
from django.utils import timezone

from nkenzapay.audit import services as audit
from nkenzapay.common.exceptions import (
    ChatLocked,
    DomainError,
    Forbidden,
    IllegalTransition,
    QuoteExpired,
)
from nkenzapay.common.money import display_amount
from nkenzapay.notifications import services as notifications

from .models import (
    CLOSED_STATUSES,
    Attachment,
    Message,
    MessageKind,
    Receipt,
    Status,
    StatusHistory,
    Transaction,
    TransactionCounter,
)
from .realtime import publish

# The legal moves. Anything not listed is refused, including moves that look
# harmless — a transfer cannot skip verification because an admin was in a hurry.
ALLOWED = {
    Status.ORDER_CREATED: {Status.AWAITING_PAYMENT, Status.CANCELLED},
    Status.AWAITING_PAYMENT: {Status.PROOF_SUBMITTED, Status.CANCELLED},
    Status.PROOF_SUBMITTED: {Status.PAYMENT_VERIFICATION, Status.CANCELLED},
    Status.PAYMENT_VERIFICATION: {Status.PAYMENT_CONFIRMED, Status.REJECTED},
    Status.PAYMENT_CONFIRMED: {Status.PAYOUT_PROCESSING},
    Status.PAYOUT_PROCESSING: {Status.PAYOUT_SENT},
    Status.PAYOUT_SENT: {Status.AWAITING_CONFIRMATION},
    Status.AWAITING_CONFIRMATION: {Status.COMPLETED, Status.DISPUTED},
    Status.DISPUTED: {Status.COMPLETED, Status.REFUND_PENDING, Status.PAYOUT_PROCESSING},
    Status.REFUND_PENDING: {Status.COMPLETED, Status.CANCELLED},
    Status.COMPLETED: set(),
    Status.CANCELLED: set(),
    Status.REJECTED: set(),
}

NOTICE_BODY = (
    "Please review your transaction details before making payment. Once a transaction "
    "has been completed and confirmed, fraudulent attempts to reverse or dispute it may "
    "result in legal action."
)


def _advance(txn, target, *, actor=None, is_system=False, note="", request=None,
             audit_action=None, audit_summary=None):
    """The only place a status changes. Assumes the row is already locked."""
    current = Status(txn.status)
    if target not in ALLOWED.get(current, set()):
        raise IllegalTransition(current.label, Status(target).label)

    txn.status = target
    fields = ["status"]

    if target in CLOSED_STATUSES:
        txn.closed_at = timezone.now()
        fields.append("closed_at")

    txn.save(update_fields=fields)

    StatusHistory.objects.create(
        transaction=txn,
        from_status=current,
        to_status=target,
        actor=actor if (actor and getattr(actor, "is_authenticated", False)) else None,
        is_system=is_system,
        note=note,
    )

    if audit_action:
        audit.record(
            actor=actor,
            action=audit_action,
            summary=audit_summary or (
                f"{txn.reference} moved from {current.label} to {Status(target).label}"
            ),
            target=txn,
            before={"status": str(current)},
            after={"status": str(target)},
            request=request,
        )

    publish(f"transaction.{txn.reference}", "status.changed", {
        "reference": txn.reference,
        "status": target,
        "label": Status(target).label,
    })
    return txn


def _locked(reference):
    return Transaction.objects.select_for_update().select_related(
        "user", "corridor__source__currency", "corridor__target__currency",
        "collect_method__instruction", "send_currency", "receive_currency",
    ).get(reference=reference)


@db_transaction.atomic
def create_transaction(*, user, quote, collect_method, recipient=None, request=None):
    """Turn a held quote into an order.

    The quote's figures are copied onto the transaction rather than referenced,
    so a later edit to fee rules or a rate refresh cannot move them.
    """
    quote = type(quote).objects.select_for_update().get(pk=quote.pk)

    if quote.is_expired:
        raise QuoteExpired()
    if quote.consumed_at is not None:
        raise DomainError("quote_used", "That quote has already been turned into an order.")
    if quote.user_id not in (None, user.id):
        raise DomainError("quote_owner", "That quote belongs to a different account.")
    if user.is_suspended:
        raise DomainError(
            "account_suspended",
            "This account cannot create transfers. Contact the desk.",
        )
    if collect_method.country_id != quote.corridor.source_id or not collect_method.is_enabled:
        raise DomainError(
            "method_unavailable",
            "That payment method is not available for this corridor.",
        )

    recipient = recipient or {}
    txn = Transaction.objects.create(
        reference=TransactionCounter.next_reference(),
        user=user,
        corridor=quote.corridor,
        direction=quote.direction,
        status=Status.ORDER_CREATED,
        quote=quote,
        rate_used=quote.rate_used,
        fee_percent=quote.fee_percent,
        send_currency=quote.send_currency,
        receive_currency=quote.receive_currency,
        send_amount=quote.send_amount,
        converted_amount=quote.converted_amount,
        fee_amount=quote.fee_amount,
        receive_amount=quote.receive_amount,
        collect_method=collect_method,
        recipient_name=recipient.get("name", ""),
        recipient_number=recipient.get("number", ""),
        recipient_details=recipient.get("details", {}),
        needs_manual_review=_needs_review(quote),
    )
    quote.consumed_at = timezone.now()
    quote.save(update_fields=["consumed_at"])

    StatusHistory.objects.create(
        transaction=txn, from_status="", to_status=Status.ORDER_CREATED,
        actor=user, note=f"Rate {quote.rate_used} stored with the order.",
    )
    audit.record(
        actor=user,
        action="transaction.created",
        summary=(
            f"{txn.reference} created for {display_amount(txn.send_amount, txn.send_currency_id)} "
            f"at rate {txn.rate_used}"
        ),
        target=txn,
        after={"reference": txn.reference, "rate": str(txn.rate_used),
               "fee_percent": str(txn.fee_percent)},
        request=request,
    )

    seed_opening_messages(txn)
    _advance(txn, Status.AWAITING_PAYMENT, actor=None, is_system=True,
             note="Payment instructions sent.")

    notifications.notify(user, "transfer.created", transaction=txn)
    notifications.notify_desk("admin.transfer_created", transaction=txn)
    return txn


def _needs_review(quote):
    from nkenzapay.pricing.engine import resolve_limit

    limit = resolve_limit(quote.corridor, quote.direction)
    return bool(
        limit
        and limit.manual_review_above is not None
        and quote.send_amount > limit.manual_review_above
    )


def seed_opening_messages(txn):
    """The two automatic messages from brief sections 21 and 34.

    Written into the thread as real rows rather than rendered on the fly, so
    what the customer was told stays readable years later even if the payment
    details change in the meantime.
    """
    Message.objects.create(
        transaction=txn,
        is_from_desk=True,
        kind=MessageKind.SYSTEM_NOTICE,
        body=NOTICE_BODY,
        payload={"heading": "Important notice", "icon": "gavel"},
    )

    instruction = txn.instruction
    rows = instruction.rows_for_chat(txn) if instruction else []
    amount = display_amount(txn.send_amount, txn.send_currency_id)
    method = txn.collect_method.label
    Message.objects.create(
        transaction=txn,
        is_from_desk=True,
        kind=MessageKind.SYSTEM_INSTRUCTIONS,
        body=(
            f"Send {amount} to the {method} account below. Upload your payment "
            f"screenshot here, then tap I have paid."
        ),
        payload={
            "rows": rows,
            "body": instruction.body if instruction else "",
            "method": method,
            "method_slug": txn.collect_method.slug,
            "qr_key": instruction.qr_key if instruction else "",
        },
    )


@db_transaction.atomic
def post_message(*, reference, sender, body, is_from_desk=False, request=None):
    txn = _locked(reference)
    if txn.chat_is_locked:
        raise ChatLocked()

    message = Message.objects.create(
        transaction=txn,
        sender=sender,
        is_from_desk=is_from_desk,
        kind=MessageKind.TEXT,
        body=body.strip(),
    )
    _publish_message(txn, message)

    preview = message.body[:120]
    if is_from_desk:
        notifications.notify(txn.user, "message.from_desk", transaction=txn,
                             context={"preview": preview})
    else:
        notifications.notify_desk("admin.message_received", transaction=txn,
                                  context={"preview": preview})
    return message


@db_transaction.atomic
def attach_file(*, reference, user, storage_key, original_name, content_type,
                size_bytes, checksum="", is_payment_proof=False, request=None):
    txn = _locked(reference)
    if txn.chat_is_locked:
        raise ChatLocked()

    message = Message.objects.create(
        transaction=txn,
        sender=user,
        is_from_desk=bool(user.is_desk),
        kind=MessageKind.ATTACHMENT,
        body="",
    )
    attachment = Attachment.objects.create(
        transaction=txn,
        message=message,
        uploaded_by=user,
        storage_key=storage_key,
        original_name=original_name,
        content_type=content_type,
        size_bytes=size_bytes,
        checksum=checksum,
        is_payment_proof=is_payment_proof,
    )
    audit.record(
        actor=user,
        action="attachment.uploaded",
        summary=f"{original_name} uploaded to {txn.reference}",
        target=txn,
        after={"attachment": attachment.pk, "proof": is_payment_proof},
        request=request,
    )
    _publish_message(txn, message)

    if not user.is_desk:
        notifications.notify_desk("admin.proof_uploaded", transaction=txn)
    return attachment


@db_transaction.atomic
def customer_paid(*, reference, user, request=None):
    """The customer says the money has left their side."""
    txn = _locked(reference)
    if txn.user_id != user.id:
        raise Forbidden("This transfer belongs to another account.", code="not_yours")
    if not txn.attachments.filter(is_payment_proof=True).exists():
        raise DomainError(
            "proof_required",
            "Upload your payment screenshot before you confirm you have paid.",
        )

    Message.objects.create(
        transaction=txn, sender=user, kind=MessageKind.ACTION,
        body="I have paid", payload={"action": "paid", "icon": "task_alt"},
    )
    _advance(txn, Status.PROOF_SUBMITTED, actor=user, request=request,
             audit_action="transaction.proof_submitted",
             audit_summary=f"Customer marked {txn.reference} as paid")
    _advance(txn, Status.PAYMENT_VERIFICATION, actor=None, is_system=True,
             note="Queued for the desk.")

    notifications.notify(user, "transfer.proof_submitted", transaction=txn)
    notifications.notify_desk("admin.customer_paid", transaction=txn)
    return txn


@db_transaction.atomic
def verify_payment(*, reference, admin_user, note="", request=None):
    """The desk has seen the money arrive."""
    txn = _locked(reference)
    _require_money_permission(admin_user)

    txn.verified_by = admin_user
    txn.verified_at = timezone.now()
    txn.save(update_fields=["verified_by", "verified_at"])

    _advance(txn, Status.PAYMENT_CONFIRMED, actor=admin_user, note=note, request=request,
             audit_action="transaction.verified",
             audit_summary=f"{admin_user.email} verified payment on {txn.reference}")
    _advance(txn, Status.PAYOUT_PROCESSING, actor=admin_user, is_system=True,
             note="Payout queued with the desk.")

    Message.objects.create(
        transaction=txn, sender=admin_user, is_from_desk=True, kind=MessageKind.ACTION,
        body="Payment verified", payload={"action": "verified", "icon": "verified"},
    )
    notifications.notify(txn.user, "transfer.payment_confirmed", transaction=txn)
    return txn


@db_transaction.atomic
def reject_payment(*, reference, admin_user, reason, request=None):
    """Refused. The reason is posted into the chat, never left implicit."""
    txn = _locked(reference)
    _require_money_permission(admin_user)
    if not reason or not reason.strip():
        raise DomainError("reason_required", "A rejection needs a reason for the customer.")

    txn.rejected_reason = reason.strip()
    txn.save(update_fields=["rejected_reason"])

    Message.objects.create(
        transaction=txn, sender=admin_user, is_from_desk=True, kind=MessageKind.TEXT,
        body=f"We could not verify this payment. {reason.strip()}",
    )
    _advance(txn, Status.REJECTED, actor=admin_user, note=reason, request=request,
             audit_action="transaction.rejected",
             audit_summary=f"{admin_user.email} rejected {txn.reference}: {reason[:120]}")

    notifications.notify(txn.user, "transfer.rejected", transaction=txn)
    return txn


@db_transaction.atomic
def mark_payout_sent(*, reference, admin_user, request=None):
    """The desk has paid the other side and is waiting on confirmation."""
    txn = _locked(reference)
    _require_money_permission(admin_user)

    txn.payout_sent_at = timezone.now()
    txn.save(update_fields=["payout_sent_at"])

    _advance(txn, Status.PAYOUT_SENT, actor=admin_user, request=request,
             audit_action="transaction.payout_sent",
             audit_summary=(
                 f"{admin_user.email} marked payout sent on {txn.reference} "
                 f"({display_amount(txn.receive_amount, txn.receive_currency_id)})"
             ))
    _advance(txn, Status.AWAITING_CONFIRMATION, actor=None, is_system=True,
             note="Waiting on the customer to confirm receipt.")

    Message.objects.create(
        transaction=txn, sender=admin_user, is_from_desk=True, kind=MessageKind.ACTION,
        body=f"Payout sent · {display_amount(txn.receive_amount, txn.receive_currency_id)}",
        payload={"action": "payout_sent", "icon": "payments",
                 "prompt": "Confirm when the money lands in your account."},
    )
    notifications.notify(txn.user, "transfer.payout_sent", transaction=txn)
    return txn


@db_transaction.atomic
def confirm_received(*, reference, user, request=None):
    """The customer confirms. This closes the transfer and cuts the receipt."""
    txn = _locked(reference)
    if txn.user_id != user.id:
        raise Forbidden("This transfer belongs to another account.", code="not_yours")

    txn.confirmed_at = timezone.now()
    txn.save(update_fields=["confirmed_at"])

    Message.objects.create(
        transaction=txn, sender=user, kind=MessageKind.ACTION,
        body="I received the money", payload={"action": "received", "icon": "task_alt"},
    )
    _advance(txn, Status.COMPLETED, actor=user, request=request,
             audit_action="transaction.completed",
             audit_summary=f"Customer confirmed receipt on {txn.reference}")

    receipt = generate_receipt(txn)
    notifications.notify(txn.user, "transfer.completed", transaction=txn)
    notifications.notify_desk("admin.customer_confirmed", transaction=txn)
    return txn, receipt


@db_transaction.atomic
def open_dispute(*, reference, user, reason_code, detail="", request=None):
    """The customer reports a problem. The transfer stops where it is."""
    from nkenzapay.disputes.models import Dispute

    txn = _locked(reference)
    if txn.user_id != user.id:
        raise Forbidden("This transfer belongs to another account.", code="not_yours")

    dispute = Dispute.objects.create(
        transaction=txn, raised_by=user, reason_code=reason_code, detail=detail
    )
    Message.objects.create(
        transaction=txn, sender=user, kind=MessageKind.ACTION,
        body=Dispute.reason_label(reason_code),
        payload={"action": "dispute", "icon": "report", "detail": detail},
    )
    if Status(txn.status) in ALLOWED and Status.DISPUTED in ALLOWED[Status(txn.status)]:
        _advance(txn, Status.DISPUTED, actor=user, note=reason_code, request=request,
                 audit_action="transaction.disputed",
                 audit_summary=f"{user.email} opened a dispute on {txn.reference}: {reason_code}")
    else:
        # A problem reported before payout does not change the status, but it
        # still opens a case so the desk sees it in the disputes queue.
        audit.record(actor=user, action="dispute.opened",
                     summary=f"{user.email} reported a problem on {txn.reference}",
                     target=txn, after={"reason": reason_code}, request=request)

    notifications.notify(user, "transfer.disputed", transaction=txn)
    notifications.notify_desk("admin.dispute_opened", transaction=txn)
    return dispute


@db_transaction.atomic
def cancel(*, reference, actor, reason="", request=None):
    txn = _locked(reference)
    _advance(txn, Status.CANCELLED, actor=actor, note=reason, request=request,
             audit_action="transaction.cancelled",
             audit_summary=f"{getattr(actor, 'email', 'system')} cancelled {txn.reference}")
    notifications.notify(txn.user, "transfer.cancelled", transaction=txn)
    return txn


def generate_receipt(txn):
    """Freeze every rendered field. A later fee change must not rewrite this."""
    from nkenzapay.common.money import display_amount as fmt

    receipt, created = Receipt.objects.get_or_create(
        transaction=txn,
        defaults={
            "number": txn.reference.replace("NKP-", "RCP-"),
            "snapshot": {
                "reference": txn.reference,
                "date": timezone.now().strftime("%d %B %Y"),
                "customer": txn.user.display_name,
                "route": txn.route_label,
                "direction": txn.direction,
                "amount_sent": fmt(txn.send_amount, txn.send_currency_id),
                "exchange_rate": str(txn.rate_used),
                "converted": fmt(txn.converted_amount, txn.receive_currency_id),
                "fee_percent": str(txn.fee_percent),
                "fee_amount": fmt(txn.fee_amount, txn.receive_currency_id),
                "amount_received": fmt(txn.receive_amount, txn.receive_currency_id),
                "payment_method": txn.collect_method.label,
                "status": Status(txn.status).label,
            },
        },
    )
    if created:
        from .receipts import queue_pdf

        queue_pdf(receipt)
    return receipt


def _require_money_permission(user):
    admin_profile = getattr(user, "admin_profile", None)
    if admin_profile is None or not admin_profile.can_move_money:
        raise Forbidden("This account cannot verify payments or send payouts.")
    if admin_profile.totp_confirmed_at is None:
        raise Forbidden(
            "Set up two-factor authentication before working on payments.",
            code="totp_required",
        )


def _publish_message(txn, message):
    publish(f"transaction.{txn.reference}", "message.created", {
        "id": message.pk,
        "kind": message.kind,
        "body": message.body,
        "is_from_desk": message.is_from_desk,
        "created_at": message.created_at.isoformat(),
    })
    publish("admin.queue", "thread.updated", {"reference": txn.reference})
