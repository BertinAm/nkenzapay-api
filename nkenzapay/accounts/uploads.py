"""Profile photo handling.

The still from the camera screen becomes the profile picture and nothing else.
It is not matched against a document, not scored for liveness, not sent to any
verification service. It exists so the desk can see who they are dealing with.
"""
from __future__ import annotations

from django.utils import timezone

from nkenzapay.common.exceptions import DomainError
from nkenzapay.common.storage import build_key, storage
from nkenzapay.common.uploads import validate_bytes, validate_declared

PHOTO_TYPES = {"image/jpeg", "image/png", "image/webp"}


def profile_photo_upload_url(user, content_type, size_bytes):
    if content_type not in PHOTO_TYPES:
        raise DomainError("unsupported_type", "A profile picture must be a JPEG, PNG or WebP.")
    validate_declared(content_type, size_bytes)
    key = build_key(f"profiles/{user.pk}", content_type)
    return storage().presign_put(key, content_type, size_bytes)


def commit_profile_photo(user, key):
    """Confirm the upload landed, verify the bytes, and swap the old one out."""
    if not key:
        raise DomainError("missing_key", "No upload key was given.")

    try:
        data = storage().read_bytes(key)
    except (FileNotFoundError, OSError) as exc:
        raise DomainError("upload_missing", "That upload did not arrive. Try again.") from exc

    content_type = "image/jpeg"
    if key.endswith(".png"):
        content_type = "image/png"
    elif key.endswith(".webp"):
        content_type = "image/webp"
    validate_bytes(data, content_type)

    profile = user.profile
    previous = profile.photo_key
    profile.photo_key = key
    profile.photo_taken_at = timezone.now()
    profile.save(update_fields=["photo_key", "photo_taken_at"])

    if previous and previous != key:
        try:
            storage().delete(previous)
        except Exception:  # noqa: BLE001 - an orphaned old photo is not worth failing over
            pass
    return profile


def store_captured_photo(user, data_url):
    """Take the canvas export straight from the capture screen."""
    from nkenzapay.common.storage import data_url_to_bytes

    try:
        data, content_type = data_url_to_bytes(data_url)
    except (ValueError, TypeError) as exc:
        raise DomainError("bad_capture", "That photo could not be read. Take it again.") from exc

    if content_type not in PHOTO_TYPES:
        content_type = "image/jpeg"
    validate_declared(content_type, len(data))
    validate_bytes(data, content_type)

    key = build_key(f"profiles/{user.pk}", content_type)
    storage().save_bytes(key, data, content_type)
    return commit_profile_photo(user, key)
