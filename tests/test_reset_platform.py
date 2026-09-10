"""Clearing the test data without clearing the setup.

The command exists for one morning: the day before launch, when the platform is
full of practice transfers and has to look new. What it must not take with it
is everything the desk spent a week configuring.
"""
import pytest
from django.core.management import call_command

pytestmark = pytest.mark.django_db


def test_it_deletes_nothing_without_confirm(receive_order, customer, seeded):
    from nkenzapay.accounts.models import User
    from nkenzapay.transactions.models import Transaction

    call_command("reset_platform", verbosity=0)

    assert Transaction.objects.count() == 1
    assert User.objects.filter(pk=customer.pk).exists()


def test_it_clears_customers_and_their_transfers(receive_order, customer, seeded):
    from nkenzapay.accounts.models import User
    from nkenzapay.transactions.models import Message, Transaction

    call_command("reset_platform", "--confirm", verbosity=0)

    assert Transaction.objects.count() == 0
    assert Message.objects.count() == 0
    assert not User.objects.filter(pk=customer.pk).exists()


def test_the_desk_keeps_its_logins(receive_order, customer, owner, seeded):
    """Wiping the account you are signed in with is not a reset, it is a
    lockout."""
    from nkenzapay.accounts.models import AdminUser, User

    call_command("reset_platform", "--confirm", verbosity=0)

    assert User.objects.filter(pk=owner.pk).exists()
    assert AdminUser.objects.filter(user=owner).exists()


def test_the_configuration_survives(receive_order, customer, configured_methods, seeded):
    """A week of setting up corridors, fees and account numbers stays."""
    from nkenzapay.geo.models import Corridor, Country
    from nkenzapay.payments.models import PaymentInstruction
    from nkenzapay.pricing.models import FeeRule, TransferLimit
    from nkenzapay.rates.models import RateProvider

    before = {
        "countries": Country.objects.count(),
        "corridors": Corridor.objects.count(),
        "fees": FeeRule.objects.count(),
        "limits": TransferLimit.objects.count(),
        "providers": RateProvider.objects.count(),
    }
    mtn = PaymentInstruction.objects.get(method__slug="mtn_momo").fields

    call_command("reset_platform", "--confirm", verbosity=0)

    assert Country.objects.count() == before["countries"]
    assert Corridor.objects.count() == before["corridors"]
    assert FeeRule.objects.count() == before["fees"]
    assert TransferLimit.objects.count() == before["limits"]
    assert RateProvider.objects.count() == before["providers"]
    # The collection account numbers customers are told to pay into.
    assert PaymentInstruction.objects.get(method__slug="mtn_momo").fields == mtn


def test_the_reference_counter_starts_again(receive_order, customer, seeded):
    """The first real transfer should be 00001, not carry on from the practice
    ones."""
    from nkenzapay.transactions.models import TransactionCounter

    call_command("reset_platform", "--confirm", verbosity=0)

    assert TransactionCounter.objects.count() == 0


def test_uploaded_files_go_with_the_rows(receive_order, customer, tmp_path, settings):
    """Otherwise the rows vanish and the photographs stay on the disk."""
    from nkenzapay.common import storage as storage_module
    from nkenzapay.transactions import services

    settings.MEDIA_ROOT = tmp_path / "private-media"
    storage_module._backend = None
    try:
        disk = storage_module.storage()
        key = "transactions/x/proof.png"
        disk.save_bytes(key, b"\x89PNG\r\n\x1a\n" + b"\x00" * 100, "image/png")
        services.attach_file(
            reference=receive_order.reference, user=customer, storage_key=key,
            original_name="proof.png", content_type="image/png",
            size_bytes=108, is_payment_proof=True,
        )
        assert disk.path_for(key).exists()

        call_command("reset_platform", "--confirm", verbosity=0)

        assert not disk.path_for(key).exists()
    finally:
        storage_module._backend = None


def test_it_is_safe_to_run_twice(receive_order, customer, seeded):
    call_command("reset_platform", "--confirm", verbosity=0)
    call_command("reset_platform", "--confirm", verbosity=0)

    from nkenzapay.transactions.models import Transaction

    assert Transaction.objects.count() == 0
