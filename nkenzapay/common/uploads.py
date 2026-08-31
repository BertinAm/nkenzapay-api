"""Upload validation.

Declared content types are a claim, not a fact. Everything that arrives is
checked twice: the declared type must be on the allowlist and within its size
cap, and the bytes themselves must start with the magic number for that type.
A .exe renamed to .png fails the second check.
"""
from __future__ import annotations

from django.conf import settings

from .exceptions import DomainError

# First bytes of each format we accept. Kept short and specific.
MAGIC = {
    "image/jpeg": [b"\xff\xd8\xff"],
    "image/png": [b"\x89PNG\r\n\x1a\n"],
    "image/webp": [b"RIFF"],
    "application/pdf": [b"%PDF-"],
    "video/mp4": [b"\x00\x00\x00", b"ftyp"],
}

# Anything that could execute if a viewer were tricked into opening it, checked
# regardless of what the file claims to be.
FORBIDDEN_MAGIC = [
    b"MZ",          # Windows executable
    b"\x7fELF",     # Linux binary
    b"#!",          # shell script
    b"PK\x03\x04",  # zip container, which includes .jar, .apk, .docm
    b"\xca\xfe\xba\xbe",  # Java class / Mach-O fat binary
]


def validate_declared(content_type: str, size_bytes: int):
    """Check a claim before handing out an upload URL."""
    allowed = settings.UPLOAD_ALLOWED_TYPES.get(content_type)
    if allowed is None:
        raise DomainError(
            "unsupported_type",
            "That file type is not accepted. Send a JPEG, PNG, WebP, PDF or MP4.",
            {"content_type": content_type},
        )
    category, _extensions = allowed
    cap = settings.UPLOAD_LIMITS[category]
    if size_bytes <= 0:
        raise DomainError("empty_file", "That file appears to be empty.")
    if size_bytes > cap:
        raise DomainError(
            "file_too_large",
            f"That file is over the {cap // (1024 * 1024)} MB limit for this type.",
            {"limit_bytes": cap},
        )
    return category


def validate_bytes(data: bytes, content_type: str):
    """Check the file itself once it has arrived."""
    head = data[:16]
    for signature in FORBIDDEN_MAGIC:
        if head.startswith(signature):
            raise DomainError(
                "executable_rejected",
                "That file looks like a program rather than a document or image.",
            )

    signatures = MAGIC.get(content_type)
    if signatures is None:
        raise DomainError("unsupported_type", "That file type is not accepted.")

    # MP4 carries its marker a few bytes in rather than at offset zero.
    if content_type == "video/mp4":
        if b"ftyp" not in data[:32]:
            raise DomainError("type_mismatch",
                              "That file does not look like the MP4 it claims to be.")
        return
    if content_type == "image/webp" and b"WEBP" not in data[:16]:
        raise DomainError("type_mismatch",
                          "That file does not look like the WebP it claims to be.")

    if not any(head.startswith(sig) for sig in signatures):
        raise DomainError(
            "type_mismatch",
            "That file does not match the type it says it is.",
        )
