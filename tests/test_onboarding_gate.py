"""Getting into the app at all.

needs_onboarding is what the front end redirects on, so what it counts decides
what somebody can reach. It used to count the name and the number alone, which
let an account with no photograph and no identity document walk all the way to
Create order before being refused.
"""
import pytest
from rest_framework.test import APIClient

from nkenzapay.accounts.serializers import UserSerializer

pytestmark = pytest.mark.django_db


def needs(user):
    return UserSerializer(user).data["needs_onboarding"]


def test_every_step_is_needed_before_the_app_opens(customer):
    profile = customer.profile
    profile.first_name = "Marie"
    profile.last_name = "Nkenganyi"
    profile.whatsapp_number = "670000000"
    profile.photo_key = ""
    profile.id_document_key = ""
    profile.save()

    # Name and number alone used to be enough. They are not.
    assert needs(customer) is True

    profile.photo_key = "profiles/1/2026/09/a.jpg"
    profile.save()
    assert needs(customer) is True, "a photo without a document is not finished"

    profile.id_document_key = "identity/1/2026/09/b.jpg"
    profile.save()
    assert needs(customer) is False


def test_a_half_finished_account_is_sent_back_to_onboarding(customer):
    """What the browser sees, not just what the serializer computes."""
    customer.set_password("a-long-enough-password")
    customer.save()
    profile = customer.profile
    profile.first_name = "Marie"
    profile.last_name = "Nkenganyi"
    profile.whatsapp_number = "670000000"
    profile.photo_key = ""
    profile.id_document_key = ""
    profile.save()

    client = APIClient()
    signed_in = client.post(
        "/api/v1/auth/login",
        {"email": customer.email, "password": "a-long-enough-password"},
        format="json",
    )

    assert signed_in.status_code == 200
    assert signed_in.json()["needs_onboarding"] is True


def test_the_desk_has_no_customer_onboarding_to_do(owner):
    """An admin account is made by another admin and never sees this flow."""
    assert needs(owner) is False
