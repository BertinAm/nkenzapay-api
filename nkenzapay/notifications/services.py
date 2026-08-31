"""Raising notifications.

One catalogue, keyed by event. Each entry decides the wording, the icon, the
action pill and whether the event is important enough to leave the app as an
email. The catalogue is data so the copy can be reviewed in one place rather
than hunted through twelve call sites.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from .models import DeliveryRule, Notification, NotificationPreference

logger = logging.getLogger(__name__)

# event -> (title template, body template, icon, action, tone, emails by default)
CATALOGUE = {
    # Account
    "account.welcome": ("Welcome to NkenzaPay",
                        "Your account is open. Finish your profile to start a transfer.",
                        "person", "", "neutral", True),
    "account.verify_email": ("Confirm your email address",
                             "Tap the link we sent to finish setting up your account.",
                             "mark_email_unread", "", "neutral", True),
    "account.password_reset": ("Password reset requested",
                               "If this was not you, contact the desk straight away.",
                               "shield_lock", "", "warn", True),
    "account.login": ("New sign-in to your account",
                      "{device} signed in. If this was not you, change your password.",
                      "login", "review", "warn", False),
    # Transfers, customer side
    "transfer.created": ("Order {reference} created",
                         "Your transfer is open. Payment instructions are in the chat.",
                         "add_circle", "open_chat", "neutral", True),
    "transfer.instructions": ("Payment instructions ready",
                              "Send {send_amount} and upload your screenshot in the chat.",
                              "payments", "open_chat", "neutral", True),
    "transfer.proof_submitted": ("We have your payment proof",
                                 "The desk is checking it now. This usually takes a few minutes.",
                                 "upload_file", "open_chat", "neutral", False),
    "transfer.payment_confirmed": ("Payment verified",
                                   "The desk confirmed your payment for {reference}.",
                                   "verified", "open_chat", "good", True),
    "transfer.payout_sent": ("Payout sent",
                             "{receive_amount} is on its way. Confirm once it lands.",
                             "payments", "confirm", "good", True),
    "transfer.completed": ("Transfer complete",
                           "{reference} is closed. Your receipt is ready to download.",
                           "task_alt", "download", "good", True),
    "transfer.rejected": ("Payment could not be verified",
                          "The desk left a reason in the chat for {reference}.",
                          "block", "open_chat", "bad", True),
    "transfer.cancelled": ("Transfer cancelled",
                           "{reference} was cancelled. Nothing was charged.",
                           "close", "", "neutral", True),
    "transfer.disputed": ("Problem reported",
                          "The desk has your report on {reference} and will reply in the chat.",
                          "report", "open_chat", "bad", True),
    "message.from_desk": ("New message from the desk",
                          "{preview}", "chat_bubble", "reply", "neutral", False),
    # Desk side
    "admin.transfer_created": ("Order created",
                              "{customer} opened {reference} for {send_amount}.",
                              "add_circle", "view", "neutral", False),
    "admin.proof_uploaded": ("Payment proof uploaded",
                             "{customer} attached a file to {reference}.",
                             "upload_file", "verify", "neutral", True),
    "admin.customer_paid": ("Customer tapped I have paid",
                            "{reference} is waiting on a verification decision.",
                            "task_alt", "verify", "neutral", True),
    "admin.message_received": ("New customer message",
                               "{customer}: {preview}", "chat_bubble", "reply", "neutral", False),
    "admin.dispute_opened": ("Problem reported",
                             "{customer} reported a problem on {reference}.",
                             "report", "open_case", "bad", True),
    "admin.customer_confirmed": ("Customer confirmed receipt",
                                 "{reference} is complete.",
                                 "verified", "view", "good", False),
    "admin.new_device_login": ("Admin sign-in from a new device",
                               "{device}. Review if this was not expected.",
                               "login", "review", "warn", True),
    "admin.rate_provider": ("Rate provider {state}",
                            "{detail}", "currency_exchange", "view", "warn", False),
}


def notify(user, event, *, transaction=None, context=None, audience=None):
    """Create one notification and send its email if the rules ask for it."""
    entry = CATALOGUE.get(event)
    if entry is None:
        logger.warning("No catalogue entry for notification event %r", event)
        return None

    title_tpl, body_tpl, icon, action, tone, emails_by_default = entry
    context = context or {}
    if transaction is not None:
        context.setdefault("reference", transaction.reference)
        context.setdefault("send_amount", _amount(transaction.send_amount,
                                                  transaction.send_currency_id))
        context.setdefault("receive_amount", _amount(transaction.receive_amount,
                                                     transaction.receive_currency_id))
        context.setdefault("customer", transaction.user.display_name)

    audience = audience or (Notification.ADMIN if event.startswith("admin.")
                            else Notification.CUSTOMER)

    notification = Notification.objects.create(
        user=user,
        audience=audience,
        event=event,
        title=_fill(title_tpl, context),
        body=_fill(body_tpl, context)[:280],
        icon=icon,
        tone=tone,
        transaction=transaction,
        action=action,
    )

    if _should_email(user, event, audience, emails_by_default):
        _send_email(notification)

    _publish(notification)
    return notification


def notify_desk(event, *, transaction=None, context=None, exclude=None):
    """Fan an event out to everyone on the desk who is allowed to see it."""
    from nkenzapay.accounts.models import AdminUser

    sent = []
    for admin in AdminUser.objects.select_related("user"):
        if exclude is not None and admin.user_id == getattr(exclude, "id", None):
            continue
        if not admin.can_chat and event.startswith("admin.message"):
            continue
        sent.append(
            notify(admin.user, event, transaction=transaction, context=context,
                   audience=Notification.ADMIN)
        )
    return sent


def _fill(template, context):
    try:
        return template.format(**context)
    except KeyError:
        # A missing placeholder should not lose the notification. Show the
        # template with the gaps left in rather than dropping the row.
        return template


def _amount(value, currency_code):
    from nkenzapay.common.money import display_amount

    return display_amount(value, currency_code)


def _should_email(user, event, audience, default):
    rule = DeliveryRule.objects.filter(event=event).first()
    if rule is not None:
        return rule.email_admins if audience == Notification.ADMIN else rule.email_customer

    group = _preference_group(event)
    if group:
        pref = NotificationPreference.objects.filter(user=user, channel_group=group).first()
        if pref is not None and not pref.is_locked:
            return pref.email
    return default


def _preference_group(event):
    if event.startswith("message"):
        return NotificationPreference.CHAT
    if event.startswith("transfer"):
        return NotificationPreference.TRANSFER_UPDATES
    if event == "account.login":
        return NotificationPreference.LOGIN
    if event.startswith(("news", "promo")):
        return NotificationPreference.MARKETING
    return ""


def _send_email(notification):
    try:
        send_mail(
            subject=notification.title,
            message=notification.body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[notification.user.email],
            fail_silently=False,
        )
    except Exception as exc:  # noqa: BLE001 - email must never break a transfer
        logger.error("Could not email notification %s: %s", notification.pk, exc)
        return
    Notification.objects.filter(pk=notification.pk).update(emailed_at=timezone.now())


def _publish(notification):
    """Push to the user's realtime channel so the bell updates without a poll."""
    from nkenzapay.transactions.realtime import publish

    publish(
        f"user.{notification.user_id}",
        "notification.created",
        {
            "id": notification.pk,
            "event": notification.event,
            "title": notification.title,
            "body": notification.body,
            "icon": notification.icon,
            "tone": notification.tone,
            "action": notification.action,
            "reference": notification.transaction.reference if notification.transaction else None,
        },
    )


def seed_preferences(user):
    for group, _label, locked in NotificationPreference.GROUPS:
        NotificationPreference.objects.get_or_create(
            user=user,
            channel_group=group,
            defaults={
                "in_app": True,
                "email": group != NotificationPreference.LOGIN,
                "is_locked": locked,
            },
        )
