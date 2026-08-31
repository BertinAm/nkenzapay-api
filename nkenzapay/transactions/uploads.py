"""Attachments on a transaction.

Access is scoped to the transaction: its customer and the desk, nobody else.
Links are signed and expire in a minute, so a URL pasted into a group chat is
worthless by the time anyone opens it.
"""
from __future__ import annotations

from django.conf import settings

from nkenzapay.common.exceptions import DomainError, Forbidden
from nkenzapay.common.storage import build_key, checksum, storage
from nkenzapay.common.uploads import validate_bytes, validate_declared

from .models import Attachment, Transaction


def request_upload_url(*, transaction, user, content_type, size_bytes, filename=""):
    if not may_access(transaction, user):
        raise Forbidden("This transfer belongs to another account.")
    if transaction.chat_is_locked:
        raise DomainError("chat_locked", "This transfer is closed. Nothing more can be added.")

    validate_declared(content_type, size_bytes)
    key = build_key(f"transactions/{transaction.reference}", content_type)
    payload = storage().presign_put(key, content_type, size_bytes)
    payload["original_name"] = filename[:255]
    return payload


def commit_upload(*, transaction, user, key, original_name, content_type,
                  size_bytes, is_payment_proof=False, request=None):
    """Verify what actually landed, then attach it to the thread."""
    from . import services

    if not may_access(transaction, user):
        raise Forbidden("This transfer belongs to another account.")

    validate_declared(content_type, size_bytes)
    try:
        data = storage().read_bytes(key)
    except (FileNotFoundError, OSError) as exc:
        raise DomainError("upload_missing", "That upload did not arrive. Try again.") from exc

    if len(data) != size_bytes:
        # The declared size is what the cap was checked against. If the bytes
        # disagree, the cap was checked against a number that meant nothing.
        validate_declared(content_type, len(data))
    validate_bytes(data, content_type)

    return services.attach_file(
        reference=transaction.reference,
        user=user,
        storage_key=key,
        original_name=original_name[:255] or "attachment",
        content_type=content_type,
        size_bytes=len(data),
        checksum=checksum(data),
        is_payment_proof=is_payment_proof,
        request=request,
    )


def signed_url_for(attachment: Attachment, user, ttl=None):
    if not may_access(attachment.transaction, user):
        raise Forbidden("That file belongs to another transfer.")
    if attachment.is_purged:
        raise DomainError(
            "file_removed",
            "This file was removed under the retention policy. The record of it "
            "stays on the transfer.",
        )
    return storage().presign_get(
        attachment.storage_key, ttl or settings.SIGNED_URL_TTL_SECONDS
    )


def may_access(transaction: Transaction, user) -> bool:
    if user is None or not user.is_authenticated:
        return False
    if transaction.user_id == user.id:
        return True
    return bool(user.is_desk)
