from decimal import Decimal

import pytest
from django.utils import timezone


@pytest.fixture
def seeded(db):
    """The platform as it launches: Cameroon, India, 6%, both minimums."""
    from django.core.management import call_command

    call_command("seed", verbosity=0)


@pytest.fixture
def configured_methods(seeded):
    """Payment details, as the desk would enter them after deploying.

    The seed deliberately leaves these blank so real collection accounts never
    live in source, which means anything exercising the chat instructions has
    to set them up first.
    """
    from nkenzapay.payments.models import PaymentInstruction

    details = {
        "mtn_momo": {"number": "6 00 000 000", "account_name": "NkenzaPay"},
        "orange_money": {"number": "6 00 000 001", "account_name": "NkenzaPay"},
        "upi": {"upi_id": "example@upi", "merchant_name": "NkenzaPay"},
    }
    for slug, fields in details.items():
        PaymentInstruction.objects.filter(method__slug=slug).update(fields=fields)
    return details


@pytest.fixture
def receive_corridor(seeded):
    from nkenzapay.geo.models import Corridor

    return Corridor.objects.get(source_id="CM", target_id="IN")


@pytest.fixture
def send_corridor(seeded):
    from nkenzapay.geo.models import Corridor

    return Corridor.objects.get(source_id="IN", target_id="CM")


@pytest.fixture
def customer(db):
    from nkenzapay.accounts.models import User

    user = User.objects.create_user(email="john@example.com", password="a-long-password-1")
    profile = user.profile
    profile.first_name = "John"
    profile.middle_name = "Doe"
    profile.last_name = "Nkenganyi"
    profile.whatsapp_country_code = "+91"
    profile.whatsapp_number = "9876543210"
    profile.completed_at = timezone.now()
    profile.save()
    return user


@pytest.fixture
def desk(db):
    from nkenzapay.accounts.models import AdminRole, AdminUser, User

    user = User.objects.create_user(email="desk@nkenzapay.com", password="a-long-password-2")
    AdminUser.objects.create(user=user, role=AdminRole.PAYMENTS,
                             totp_confirmed_at=timezone.now())
    return user


@pytest.fixture
def support_only(db):
    from nkenzapay.accounts.models import AdminRole, AdminUser, User

    user = User.objects.create_user(email="support@nkenzapay.com",
                                    password="a-long-password-3")
    AdminUser.objects.create(user=user, role=AdminRole.SUPPORT,
                             totp_confirmed_at=timezone.now())
    return user


@pytest.fixture
def receive_order(receive_corridor, configured_methods, customer):
    """A live Cameroon to India transfer, awaiting payment."""
    from nkenzapay.payments.models import PaymentMethod
    from nkenzapay.pricing.engine import build_quote, persist_quote
    from nkenzapay.transactions import services

    result = build_quote(corridor=receive_corridor, direction="receive",
                         send_amount=Decimal("100000"), user=customer)
    quote = persist_quote(result, user=customer)
    return services.create_transaction(
        user=customer,
        quote=quote,
        collect_method=PaymentMethod.objects.get(slug="mtn_momo"),
    )
