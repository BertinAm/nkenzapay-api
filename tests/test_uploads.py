"""Upload validation.

A declared content type is a claim. These tests are the reason the platform
does not accept the claim on its own.
"""
import pytest

from nkenzapay.common.exceptions import DomainError
from nkenzapay.common.uploads import validate_bytes, validate_declared

pytestmark = pytest.mark.django_db

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
PDF = b"%PDF-1.7\n" + b"\x00" * 200
EXE = b"MZ\x90\x00" + b"\x00" * 200


def test_an_allowed_type_passes():
    assert validate_declared("image/png", 4000) == "image"
    validate_bytes(PNG, "image/png")


def test_an_unlisted_type_is_refused():
    with pytest.raises(DomainError) as exc:
        validate_declared("image/svg+xml", 4000)
    assert exc.value.code == "unsupported_type"


def test_an_oversized_image_is_refused():
    with pytest.raises(DomainError) as exc:
        validate_declared("image/jpeg", 11 * 1024 * 1024)
    assert exc.value.code == "file_too_large"


def test_video_gets_the_larger_cap():
    assert validate_declared("video/mp4", 40 * 1024 * 1024) == "video"
    with pytest.raises(DomainError):
        validate_declared("video/mp4", 60 * 1024 * 1024)


def test_an_empty_file_is_refused():
    with pytest.raises(DomainError) as exc:
        validate_declared("image/png", 0)
    assert exc.value.code == "empty_file"


def test_an_executable_renamed_to_png_is_caught():
    """The name and the declared type both say image. The bytes do not."""
    validate_declared("image/png", len(EXE))
    with pytest.raises(DomainError) as exc:
        validate_bytes(EXE, "image/png")
    assert exc.value.code == "executable_rejected"


def test_a_zip_disguised_as_a_pdf_is_caught():
    zipped = b"PK\x03\x04" + b"\x00" * 100
    with pytest.raises(DomainError) as exc:
        validate_bytes(zipped, "application/pdf")
    assert exc.value.code == "executable_rejected"


def test_a_jpeg_claiming_to_be_a_pdf_is_caught():
    with pytest.raises(DomainError) as exc:
        validate_bytes(JPEG, "application/pdf")
    assert exc.value.code == "type_mismatch"


def test_a_real_pdf_passes():
    validate_bytes(PDF, "application/pdf")


def test_a_shell_script_is_refused():
    with pytest.raises(DomainError):
        validate_bytes(b"#!/bin/sh\nrm -rf /", "image/png")


# --- access scoping --------------------------------------------------------


def test_a_stranger_cannot_reach_an_attachment(receive_order, customer, db):
    from nkenzapay.accounts.models import User
    from nkenzapay.transactions import services
    from nkenzapay.transactions.uploads import signed_url_for

    attachment = services.attach_file(
        reference=receive_order.reference, user=customer,
        storage_key="test/proof.png", original_name="proof.png",
        content_type="image/png", size_bytes=400, is_payment_proof=True,
    )
    stranger = User.objects.create_user(email="nosy@example.com",
                                        password="a-long-password-5")

    with pytest.raises(DomainError) as exc:
        signed_url_for(attachment, stranger)
    assert exc.value.code == "forbidden"


def test_the_owner_and_the_desk_can_reach_it(receive_order, customer, desk):
    from nkenzapay.transactions import services
    from nkenzapay.transactions.uploads import signed_url_for

    attachment = services.attach_file(
        reference=receive_order.reference, user=customer,
        storage_key="test/proof.png", original_name="proof.png",
        content_type="image/png", size_bytes=400, is_payment_proof=True,
    )
    assert signed_url_for(attachment, customer)
    assert signed_url_for(attachment, desk)


def test_a_local_signed_link_expires():
    from nkenzapay.common.storage import LocalStorage

    backend = LocalStorage()
    signed = backend.signer.sign("transactions/x/file.png")
    assert backend.verify_signed_key(signed, ttl=60) == "transactions/x/file.png"
    assert backend.verify_signed_key(signed, ttl=-1) is None


def test_a_storage_key_cannot_escape_the_media_root():
    from nkenzapay.common.storage import LocalStorage

    with pytest.raises(ValueError):
        LocalStorage().path_for("../../etc/passwd")
