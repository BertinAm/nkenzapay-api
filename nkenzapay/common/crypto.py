"""Encryption for files at rest.

The deployment target is shared hosting. The disk under the account is not
private in the way a dedicated machine's disk is: the host's staff can read it,
their backup system copies it somewhere else, and a file-read bug in any other
application on the same account reaches it too. Payment evidence and
photographs of customers' faces are the worst possible things to leave sitting
there in the clear.

So they are not. Every file written by the local backend is sealed with
AES-256-GCM before it touches the disk, and the key lives in the environment
rather than in the filesystem. A copy of the disk without the environment is a
directory full of noise.

The key path is bound into the ciphertext as additional data, so a sealed file
cannot be renamed into another customer's slot and still open: move it and the
tag check fails.

Rotation is a keyring. The first key seals new writes; every key can open what
it sealed, so an old key stays until nothing needs it. `manage.py encrypt_media`
rewrites everything under the active key when you want it gone.
"""
from __future__ import annotations

import base64
import logging
import os
import secrets

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger(__name__)

MAGIC = b"NKZ1"
VERSION = 1
NONCE_BYTES = 12
KEY_BYTES = 32


class DecryptionError(Exception):
    """The bytes are sealed with a key this deployment does not hold."""


def generate_key() -> str:
    """A new base64 key, ready to paste into the environment."""
    return base64.b64encode(secrets.token_bytes(KEY_BYTES)).decode()


def _decode(value: str) -> bytes:
    try:
        raw = base64.b64decode(value, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise ImproperlyConfigured(
            "A media encryption key is not valid base64. "
            "Generate one with: manage.py generate_media_key"
        ) from exc
    if len(raw) != KEY_BYTES:
        raise ImproperlyConfigured(
            f"A media encryption key must decode to {KEY_BYTES} bytes, "
            f"not {len(raw)}. Generate one with: manage.py generate_media_key"
        )
    return raw


def keyring() -> dict[str, bytes]:
    """Every key this deployment can open a file with, active one first.

    Configured as `MEDIA_ENCRYPTION_KEYS=id:base64,older:base64`, or as a bare
    `MEDIA_ENCRYPTION_KEY=base64` when there has never been a rotation.
    """
    entries = getattr(settings, "MEDIA_ENCRYPTION_KEYS", None) or []
    ring: dict[str, bytes] = {}

    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue
        identifier, _, value = entry.partition(":")
        if not value:
            identifier, value = "k1", identifier
        ring[identifier.strip()[:16] or "k1"] = _decode(value.strip())

    single = getattr(settings, "MEDIA_ENCRYPTION_KEY", "")
    if single and not ring:
        ring["k1"] = _decode(single.strip())

    return ring


def is_configured() -> bool:
    return bool(keyring())


def active_key() -> tuple[str, bytes]:
    ring = keyring()
    if not ring:
        raise ImproperlyConfigured(
            "MEDIA_ENCRYPTION_KEY is not set, so uploaded files would be written "
            "to disk in the clear. Generate one with: manage.py generate_media_key"
        )
    identifier = next(iter(ring))
    return identifier, ring[identifier]


def looks_sealed(data: bytes) -> bool:
    return data[:4] == MAGIC


def seal(data: bytes, *, aad: str) -> bytes:
    """Encrypt, tagging the result with the key path it belongs at."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    identifier, key = active_key()
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(key).encrypt(nonce, data, aad.encode())

    label = identifier.encode()
    return b"".join([MAGIC, bytes([VERSION, len(label)]), label, nonce, ciphertext])


def open_sealed(data: bytes, *, aad: str) -> bytes:
    """Decrypt. Anything not sealed is returned as it is.

    Files written before a key was configured are plaintext and still readable;
    `manage.py encrypt_media` seals them. Refusing to read them instead would
    mean a deployment that turned encryption on lost access to its own history.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not looks_sealed(data):
        return data

    version = data[4]
    if version != VERSION:
        raise DecryptionError(f"Unknown media encryption version {version}.")

    label_length = data[5]
    cursor = 6 + label_length
    identifier = data[6:cursor].decode(errors="replace")
    nonce = data[cursor:cursor + NONCE_BYTES]
    ciphertext = data[cursor + NONCE_BYTES:]

    key = keyring().get(identifier)
    if key is None:
        raise DecryptionError(
            f"This file was sealed with key '{identifier}', which is not in "
            "MEDIA_ENCRYPTION_KEYS. Add the old key back to read it."
        )

    try:
        return AESGCM(key).decrypt(nonce, ciphertext, aad.encode())
    except Exception as exc:  # noqa: BLE001
        raise DecryptionError(
            "This file could not be opened. It is damaged, or it was moved to a "
            "path other than the one it was sealed for."
        ) from exc
