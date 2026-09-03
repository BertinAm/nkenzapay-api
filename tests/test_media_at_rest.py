"""What ends up on the disk, and what does not.

The deployment writes payment evidence and photographs of customers' faces to
shared hosting. The disk is read by the host's staff, copied by their backup
system, and reachable from anything else running on the account, so the only
useful assumption is that somebody else can read it. These tests say what they
would find.
"""
import base64
import os

import pytest
from django.core.checks import Error
from django.core.management import call_command

from nkenzapay.common import crypto

KEY = base64.b64encode(b"k" * 32).decode()
OTHER_KEY = base64.b64encode(b"j" * 32).decode()

PAYLOAD = b"\xff\xd8\xff a payment screenshot, as far as anyone else is concerned"


@pytest.fixture
def disk(tmp_path, settings):
    """A media root of this test's own, with encryption configured.

    The backend is a module-level singleton, so it is dropped here and rebuilt
    against the overridden root. Otherwise a management command under test
    would happily sweep the developer's real one.
    """
    from nkenzapay.common import storage as storage_module

    settings.MEDIA_ROOT = tmp_path / "private-media"
    settings.MEDIA_ENCRYPTION_KEY = KEY
    settings.MEDIA_ENCRYPTION_KEYS = []

    storage_module._backend = None
    yield storage_module.storage()
    storage_module._backend = None


# --- what is written ------------------------------------------------------


def test_the_bytes_on_disk_are_not_the_bytes_that_were_uploaded(disk):
    disk.save_bytes("transactions/NKP-1/proof.jpg", PAYLOAD)

    on_disk = disk.path_for("transactions/NKP-1/proof.jpg").read_bytes()
    assert PAYLOAD not in on_disk
    assert b"payment screenshot" not in on_disk
    assert crypto.looks_sealed(on_disk)


def test_the_application_still_reads_what_it_wrote(disk):
    disk.save_bytes("transactions/NKP-1/proof.jpg", PAYLOAD)
    assert disk.read_bytes("transactions/NKP-1/proof.jpg") == PAYLOAD


def test_a_copy_of_the_disk_without_the_key_is_worthless(disk, settings):
    disk.save_bytes("profiles/7/face.jpg", PAYLOAD)

    # The host's backup, restored somewhere the environment did not follow.
    settings.MEDIA_ENCRYPTION_KEY = OTHER_KEY
    with pytest.raises(crypto.DecryptionError):
        disk.read_bytes("profiles/7/face.jpg")


def test_a_file_moved_into_another_customers_slot_will_not_open(disk):
    """The key path is sealed into the ciphertext.

    Somebody with write access to the disk but not the key could otherwise
    swap one customer's identity photograph for another's and let the
    application serve it to the wrong person.
    """
    disk.save_bytes("profiles/7/face.jpg", PAYLOAD)
    stolen = disk.path_for("profiles/7/face.jpg").read_bytes()

    disk.save_bytes("profiles/9/face.jpg", b"\xff\xd8\xff something else")
    disk.path_for("profiles/9/face.jpg").write_bytes(stolen)

    with pytest.raises(crypto.DecryptionError):
        disk.read_bytes("profiles/9/face.jpg")


def test_tampering_with_a_sealed_file_is_caught(disk):
    disk.save_bytes("transactions/NKP-1/proof.pdf", PAYLOAD)
    path = disk.path_for("transactions/NKP-1/proof.pdf")

    damaged = bytearray(path.read_bytes())
    damaged[-1] ^= 0xFF
    path.write_bytes(bytes(damaged))

    with pytest.raises(crypto.DecryptionError):
        disk.read_bytes("transactions/NKP-1/proof.pdf")


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_nothing_is_readable_by_anyone_else_on_the_machine(disk):
    disk.save_bytes("transactions/NKP-1/proof.jpg", PAYLOAD)
    path = disk.path_for("transactions/NKP-1/proof.jpg")

    assert path.stat().st_mode & 0o077 == 0, "group and other can read this"
    assert path.parent.stat().st_mode & 0o077 == 0


def test_the_media_root_carries_a_web_server_deny(disk):
    disk.save_bytes("transactions/NKP-1/proof.jpg", PAYLOAD)
    guard = (disk.root / ".htaccess").read_text()
    assert "denied" in guard.lower()


def test_a_key_written_before_encryption_was_turned_on_still_opens(disk, settings):
    """A deployment that adds a key later must not lose its own history."""
    path = disk.path_for("transactions/NKP-2/old.jpg")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PAYLOAD)

    assert disk.read_bytes("transactions/NKP-2/old.jpg") == PAYLOAD


def test_rotation_keeps_old_files_readable(disk, settings):
    disk.save_bytes("transactions/NKP-3/first.jpg", PAYLOAD)

    settings.MEDIA_ENCRYPTION_KEYS = [f"k2:{OTHER_KEY}", f"k1:{KEY}"]
    settings.MEDIA_ENCRYPTION_KEY = ""

    disk.save_bytes("transactions/NKP-3/second.jpg", b"\xff\xd8\xff newer")

    assert disk.read_bytes("transactions/NKP-3/first.jpg") == PAYLOAD
    assert disk.read_bytes("transactions/NKP-3/second.jpg") == b"\xff\xd8\xff newer"


def test_a_malformed_key_is_refused_rather_than_used(settings):
    from django.core.exceptions import ImproperlyConfigured

    settings.MEDIA_ENCRYPTION_KEY = "not-base64-at-all!!"
    settings.MEDIA_ENCRYPTION_KEYS = []
    with pytest.raises(ImproperlyConfigured):
        crypto.keyring()

    settings.MEDIA_ENCRYPTION_KEY = base64.b64encode(b"too short").decode()
    with pytest.raises(ImproperlyConfigured):
        crypto.keyring()


def test_a_generated_key_works(settings):
    settings.MEDIA_ENCRYPTION_KEYS = []
    settings.MEDIA_ENCRYPTION_KEY = crypto.generate_key()
    sealed = crypto.seal(b"hello", aad="a/b")
    assert crypto.open_sealed(sealed, aad="a/b") == b"hello"


# --- the commands ---------------------------------------------------------


def test_encrypt_media_seals_what_was_left_in_the_clear(disk, settings, capsys):
    path = disk.path_for("transactions/NKP-4/plain.jpg")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PAYLOAD)

    call_command("encrypt_media")

    assert crypto.looks_sealed(path.read_bytes())
    assert disk.read_bytes("transactions/NKP-4/plain.jpg") == PAYLOAD


def test_encrypt_media_leaves_already_sealed_files_alone(disk):
    disk.save_bytes("transactions/NKP-5/proof.jpg", PAYLOAD)
    before = disk.path_for("transactions/NKP-5/proof.jpg").read_bytes()

    call_command("encrypt_media")

    assert disk.path_for("transactions/NKP-5/proof.jpg").read_bytes() == before


def test_the_sweep_removes_an_abandoned_upload(disk, settings, db):
    """Bytes arrive, the customer closes the tab, nothing ever attaches them.

    Nothing reads them and nothing deletes them, and nobody has checked what
    they are - validation happens at the attach step, which never came.
    """
    settings.UPLOAD_ORPHAN_HOURS = 0
    disk.save_bytes("transactions/NKP-6/abandoned.jpg", PAYLOAD)

    call_command("sweep_media", "--orphans-only")

    assert not disk.path_for("transactions/NKP-6/abandoned.jpg").exists()


def test_the_sweep_leaves_a_file_a_row_points_at(disk, settings, receive_order,
                                                 customer, db):
    from nkenzapay.transactions.models import Attachment

    settings.UPLOAD_ORPHAN_HOURS = 0
    key = "transactions/NKP-7/proof.jpg"
    disk.save_bytes(key, PAYLOAD)
    Attachment.objects.create(
        transaction=receive_order, uploaded_by=customer, storage_key=key,
        original_name="proof.jpg", content_type="image/jpeg", size_bytes=len(PAYLOAD),
    )

    call_command("sweep_media", "--orphans-only")

    assert disk.path_for(key).exists()


def test_the_sweep_leaves_a_file_that_only_just_arrived(disk, settings, db):
    """One may be mid-flight between the upload and the call that attaches it."""
    settings.UPLOAD_ORPHAN_HOURS = 24
    disk.save_bytes("transactions/NKP-8/in-flight.jpg", PAYLOAD)

    call_command("sweep_media", "--orphans-only")

    assert disk.path_for("transactions/NKP-8/in-flight.jpg").exists()


def test_retention_removes_the_file_and_keeps_the_record(disk, settings,
                                                         receive_order, customer, db):
    from datetime import timedelta

    from django.utils import timezone

    from nkenzapay.transactions.models import Attachment

    settings.MEDIA_RETENTION_DAYS = 30
    key = "transactions/NKP-9/proof.jpg"
    disk.save_bytes(key, PAYLOAD)
    attachment = Attachment.objects.create(
        transaction=receive_order, uploaded_by=customer, storage_key=key,
        original_name="proof.jpg", content_type="image/jpeg", size_bytes=len(PAYLOAD),
        is_payment_proof=True,
    )
    receive_order.closed_at = timezone.now() - timedelta(days=90)
    receive_order.save(update_fields=["closed_at"])

    call_command("sweep_media")

    attachment.refresh_from_db()
    assert not disk.path_for(key).exists()
    assert attachment.purged_at is not None
    assert attachment.original_name == "proof.jpg", "the record of it stays"
    assert attachment.is_purged


def test_retention_leaves_an_open_transfer_alone(disk, settings, receive_order,
                                                 customer, db):
    from nkenzapay.transactions.models import Attachment

    settings.MEDIA_RETENTION_DAYS = 30
    key = "transactions/NKP-10/proof.jpg"
    disk.save_bytes(key, PAYLOAD)
    Attachment.objects.create(
        transaction=receive_order, uploaded_by=customer, storage_key=key,
        original_name="proof.jpg", content_type="image/jpeg", size_bytes=len(PAYLOAD),
    )

    call_command("sweep_media")

    assert disk.path_for(key).exists()


def test_a_purged_attachment_is_not_offered_as_a_link(disk, receive_order,
                                                      customer, db):
    from nkenzapay.common.exceptions import DomainError
    from nkenzapay.transactions.models import Attachment
    from nkenzapay.transactions.uploads import signed_url_for

    attachment = Attachment.objects.create(
        transaction=receive_order, uploaded_by=customer, storage_key="",
        original_name="proof.jpg", content_type="image/jpeg", size_bytes=10,
        purged_at=receive_order.created_at,
    )

    with pytest.raises(DomainError):
        signed_url_for(attachment, customer)


# --- the deployment checks ------------------------------------------------


def test_the_deploy_check_refuses_an_unencrypted_disk(settings):
    from nkenzapay.common.checks import check_media_is_encrypted

    settings.MEDIA_ENCRYPTION_KEY = ""
    settings.MEDIA_ENCRYPTION_KEYS = []
    settings.MEDIA_STORAGE = "local"

    problems = check_media_is_encrypted(None)
    assert [p.id for p in problems] == ["nkenzapay.E002"]
    assert all(isinstance(p, Error) for p in problems)


def test_the_deploy_check_passes_once_a_key_is_set(settings):
    from nkenzapay.common.checks import check_media_is_encrypted

    settings.MEDIA_ENCRYPTION_KEY = KEY
    settings.MEDIA_ENCRYPTION_KEYS = []
    settings.MEDIA_STORAGE = "local"

    assert check_media_is_encrypted(None) == []


def test_the_deploy_check_catches_a_media_root_the_web_server_serves(settings,
                                                                     tmp_path):
    from nkenzapay.common.checks import check_media_root_is_not_web_served

    root = tmp_path / "public_html" / "private-media"
    root.mkdir(parents=True)
    settings.MEDIA_ROOT = root

    problems = check_media_root_is_not_web_served(None)
    assert [p.id for p in problems] == ["nkenzapay.E003"]


def test_the_deploy_check_notices_a_broken_cache(settings, monkeypatch):
    """The failure that 500'd every request. It should cost a line of output
    at deploy time instead."""
    from django.core import cache as cache_module

    from nkenzapay.common.checks import check_the_cache_works

    class Broken:
        def set(self, *args, **kwargs):
            raise RuntimeError("no such table: django_cache")

        def get(self, *args, **kwargs):
            raise RuntimeError("no such table: django_cache")

        def delete(self, *args, **kwargs):
            raise RuntimeError("no such table: django_cache")

    monkeypatch.setattr(cache_module, "cache", Broken())

    problems = check_the_cache_works(None)
    assert [p.id for p in problems] == ["nkenzapay.E004"]
    assert "createcachetable" in problems[0].hint


def test_the_deploy_check_warns_when_the_callers_address_is_unknowable(settings):
    """Behind a proxy with no trusted header, one attacker would look like
    everybody. That is why loopback is never auto-blocked, and this says so
    before the situation arises rather than after."""
    from nkenzapay.common.checks import check_the_client_address_is_knowable

    settings.TRUSTED_IP_HEADERS = []
    assert [p.id for p in check_the_client_address_is_knowable(None)] == [
        "nkenzapay.W006"
    ]

    settings.TRUSTED_IP_HEADERS = ["HTTP_CF_CONNECTING_IP"]
    assert check_the_client_address_is_knowable(None) == []


def test_the_deploy_check_catches_smtp_selected_but_not_configured(settings):
    """The failure this exists for is silent: Django posts to localhost:25 and
    logs, while the customer waits for a reset that is never coming."""
    from nkenzapay.common.checks import check_mail_can_actually_be_sent

    settings.EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    settings.EMAIL_HOST = ""
    assert "nkenzapay.E013" in [
        p.id for p in check_mail_can_actually_be_sent(None)
    ]

    settings.EMAIL_HOST = "mail.example.com"
    settings.EMAIL_USE_TLS = True
    settings.EMAIL_USE_SSL = False
    assert check_mail_can_actually_be_sent(None) == []


def test_the_deploy_check_refuses_tls_and_ssl_together(settings):
    from nkenzapay.common.checks import check_mail_can_actually_be_sent

    settings.EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    settings.EMAIL_HOST = "mail.example.com"
    settings.EMAIL_USE_TLS = True
    settings.EMAIL_USE_SSL = True
    assert "nkenzapay.E014" in [
        p.id for p in check_mail_can_actually_be_sent(None)
    ]


def test_the_console_backend_is_left_alone(settings):
    """Nothing to warn about when mail is only being printed."""
    from nkenzapay.common.checks import check_mail_can_actually_be_sent

    settings.EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
    settings.EMAIL_HOST = ""
    assert check_mail_can_actually_be_sent(None) == []


def test_the_deploy_check_refuses_one_key_doing_two_jobs(settings):
    from nkenzapay.common.checks import check_secrets_are_real

    settings.SECRET_KEY = "a-real-looking-secret-key-value-that-is-long"
    settings.MEDIA_ENCRYPTION_KEY = settings.SECRET_KEY

    assert [p.id for p in check_secrets_are_real(None)] == ["nkenzapay.E008"]
