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


# --- the endpoint that does the writing ------------------------------------
#
# Everything above tests the rules. These test the write, through the URL a
# browser actually calls, because the rules were never reached: the route used
# a path converter that stops at the first slash, and a storage key is a
# directory path, so every upload in production 404ed before any view ran.

BODY = b"\xff\xd8\xff\xe0" + b"\x00" * 500
KEY = "profiles/7/2026/09/abcdef.jpg"


@pytest.fixture
def disk(tmp_path, settings):
    """A media root of this test's own, with the singleton pointed at it."""
    from nkenzapay.common import storage as storage_module

    settings.MEDIA_ROOT = tmp_path / "private-media"
    storage_module._backend = None
    yield storage_module.storage()
    storage_module._backend = None


def test_an_upload_url_resolves_and_the_file_lands(disk, client):
    grant = disk.presign_put(KEY, "image/jpeg", len(BODY))

    response = client.put(grant["url"], data=BODY, content_type="image/jpeg")

    assert response.status_code == 201
    assert disk.read_bytes(KEY) == BODY


def test_a_signed_read_link_resolves_too(disk, client):
    disk.save_bytes(KEY, BODY, "image/jpeg")

    response = client.get(disk.presign_get(KEY))

    assert response.status_code == 200
    assert b"".join(response.streaming_content) == BODY


def test_a_read_link_lasts_as_long_as_it_was_issued_for(disk, settings):
    """Five minutes has to mean five minutes.

    presign_get took a ttl and dropped it, so the endpoint fell back to the
    global sixty seconds for every link. The desk screen offers five minutes,
    so that somebody has time to actually read a passport, and got one.
    """
    import time
    from unittest import mock

    settings.SIGNED_URL_TTL_SECONDS = 60
    # Split on the prefix, not on the last slash: a read token carries the key,
    # and a key is a path.
    prefix = "/api/v1/uploads/local/"
    long_link = disk.presign_get(KEY, ttl=300).split(prefix, 1)[1]
    default_link = disk.presign_get(KEY).split(prefix, 1)[1]

    assert disk.verify_signed_key(long_link) == KEY
    assert disk.verify_signed_key(default_link) == KEY

    # Two minutes on: the five-minute link still opens, the default one does not.
    later = time.time() + 120
    with mock.patch.object(time, "time", lambda: later):
        assert disk.verify_signed_key(long_link) == KEY
        assert disk.verify_signed_key(default_link) is None


def test_a_read_link_cannot_be_used_to_write(disk, client):
    """The link that shows somebody their own photo must not replace it.

    Both directions were signed with the same salt, so the sixty-second URL
    handed out to view a payment proof was, character for character, a URL that
    could overwrite it.
    """
    disk.save_bytes(KEY, BODY, "image/jpeg")

    response = client.put(disk.presign_get(KEY), data=b"\xff\xd8\xff replaced",
                          content_type="image/jpeg")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_upload_url"
    assert disk.read_bytes(KEY) == BODY


def test_an_upload_past_its_ceiling_is_refused(disk, client):
    grant = disk.presign_put(KEY, "image/jpeg", 100)

    response = client.put(grant["url"], data=BODY, content_type="image/jpeg")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "file_too_large"
    assert not disk.path_for(KEY).exists()


def test_bytes_that_contradict_the_granted_type_are_refused(disk, client):
    """The grant said PNG. A header saying so does not make it one."""
    grant = disk.presign_put("profiles/7/2026/09/x.png", "image/png", len(EXE))

    response = client.put(grant["url"], data=EXE, content_type="image/png")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "executable_rejected"
    assert not disk.path_for("profiles/7/2026/09/x.png").exists()


def test_an_empty_upload_is_refused(disk, client):
    grant = disk.presign_put(KEY, "image/jpeg", 1000)

    response = client.put(grant["url"], data=b"", content_type="image/jpeg")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "empty_file"


def test_the_grant_is_inside_the_signature(disk):
    """The type and the ceiling are signed, not carried beside the signature."""
    grant = disk.presign_put(KEY, "image/jpeg", 100)
    token = grant["url"].rsplit("/", 1)[1]

    assert disk.verify_upload_token(token) == (KEY, "image/jpeg", 100)
    assert disk.verify_upload_token(token, ttl=-1) is None

    # Re-pack the same key with a bigger ceiling and a type of one's choosing.
    # Without the signing key it does not verify, which is the whole point.
    from nkenzapay.common.storage import _pack

    forged = _pack(KEY, "application/pdf", 500 * 1024 * 1024)
    assert disk.verify_upload_token(f"{forged}:1x0000:notarealsignature") is None


def test_an_upload_token_is_url_safe(disk):
    """No character in the token needs encoding by a browser, CDN or proxy."""
    import re

    token = disk.presign_put(KEY, "image/jpeg", 100)["url"].rsplit("/", 1)[1]

    assert re.fullmatch(r"[A-Za-z0-9_\-:]+", token)
