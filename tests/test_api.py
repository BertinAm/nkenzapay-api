"""The HTTP surface.

Checks the shapes the front end actually consumes, and the boundaries that
matter: an anonymous visitor may price a transfer but not open one, and one
customer may never read another's thread.
"""
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def signed_in(api, customer):
    api.force_authenticate(customer)
    return api


def test_a_visitor_can_price_a_transfer(api, receive_corridor):
    response = api.post("/api/v1/rates/quote", {
        "source": "CM", "target": "IN", "direction": "receive",
        "send_amount": "100,000",
    }, format="json")

    assert response.status_code == 200
    body = response.json()
    assert body["receive_amount"]["display"] == "₹15,918.90"
    assert body["converted_amount"]["display"] == "₹16,935.00"
    assert body["fee_amount"]["display"] == "₹1,016.10"
    assert body["reference"] is None


def test_a_signed_in_customer_gets_a_holdable_quote(signed_in, receive_corridor):
    response = signed_in.post("/api/v1/rates/quote", {
        "source": "CM", "target": "IN", "direction": "receive",
        "send_amount": "100000",
    }, format="json")

    body = response.json()
    assert body["reference"].startswith("Q")
    assert 55 <= body["seconds_remaining"] <= 61


def test_a_quote_under_the_minimum_returns_the_message_not_an_error(api, receive_corridor):
    response = api.post("/api/v1/rates/quote", {
        "source": "CM", "target": "IN", "direction": "receive",
        "send_amount": "1000",
    }, format="json")

    assert response.status_code == 200
    body = response.json()
    assert body["errors"][0]["code"] == "below_minimum"
    assert body["reference"] is None


def test_a_closed_corridor_is_refused(api, seeded):
    response = api.post("/api/v1/rates/quote", {
        "source": "NG", "target": "IN", "direction": "receive",
        "send_amount": "100000",
    }, format="json")
    assert response.status_code == 400


def test_creating_an_order_requires_an_account(api, receive_corridor):
    response = api.post("/api/v1/transactions", {
        "quote_reference": "QDEADBEEF", "collect_method": "mtn_momo",
    }, format="json")
    assert response.status_code in (401, 403)


def test_an_order_can_be_created_from_a_quote(signed_in, receive_corridor):
    quote = signed_in.post("/api/v1/rates/quote", {
        "source": "CM", "target": "IN", "direction": "receive",
        "send_amount": "100000",
    }, format="json").json()

    response = signed_in.post("/api/v1/transactions", {
        "quote_reference": quote["reference"], "collect_method": "mtn_momo",
    }, format="json")

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "awaiting_payment"
    assert body["receive"]["display"] == "₹15,918.90"
    assert len(body["stepper"]) == 6


def test_a_send_order_needs_recipient_details(signed_in, send_corridor):
    quote = signed_in.post("/api/v1/rates/quote", {
        "source": "IN", "target": "CM", "direction": "send",
        "send_amount": "10000",
    }, format="json").json()

    response = signed_in.post("/api/v1/transactions", {
        "quote_reference": quote["reference"], "collect_method": "upi",
    }, format="json")
    assert response.status_code == 400
    assert "recipient_name" in response.json()["error"]["detail"]


def test_one_customer_cannot_read_anothers_transfer(api, receive_order, db):
    from nkenzapay.accounts.models import User

    stranger = User.objects.create_user(email="nosy2@example.com",
                                        password="a-long-password-6")
    api.force_authenticate(stranger)

    assert api.get(f"/api/v1/transactions/{receive_order.reference}").status_code == 404
    assert api.get(
        f"/api/v1/transactions/{receive_order.reference}/messages"
    ).status_code == 404


def test_the_owner_reads_their_own_thread(api, receive_order, customer):
    api.force_authenticate(customer)
    response = api.get(f"/api/v1/transactions/{receive_order.reference}/messages")
    assert response.status_code == 200
    assert len(response.json()) == 2


def test_the_public_method_list_masks_the_account_number(api, configured_methods):
    response = api.get("/api/v1/payments/methods?country=CM&side=collect")
    assert response.status_code == 200

    mtn = next(m for m in response.json() if m["slug"] == "mtn_momo")
    number = next(d["value"] for d in mtn["masked_details"] if d["label"] == "Number")
    assert "•" in number
    assert configured_methods["mtn_momo"]["number"] not in number


def test_a_disabled_method_never_appears(api, seeded):
    slugs = [m["slug"] for m in api.get("/api/v1/payments/methods?country=IN").json()]
    assert "cbdc" not in slugs
    assert "upi" in slugs


def test_countries_carry_their_availability(api, seeded):
    rows = {c["iso2"]: c for c in api.get("/api/v1/geo/countries").json()}
    assert rows["CM"]["is_enabled"] is True
    assert rows["NG"]["is_enabled"] is False


def test_an_owner_can_add_a_country_with_its_corridors(api, seeded, db):
    """A country with no corridors cannot be traded either way, so adding one
    by hand afterwards would be a step somebody forgets."""
    from django.utils import timezone

    from nkenzapay.accounts.models import AdminRole, AdminUser, User
    from nkenzapay.geo.models import Corridor, Country

    owner = User.objects.create_user(email="owner@nkenzapay.com",
                                     password="a-long-password-9")
    AdminUser.objects.create(user=owner, role=AdminRole.OWNER,
                             totp_confirmed_at=timezone.now())
    api.force_authenticate(owner)

    response = api.post("/api/v1/admin/countries",
                        {"iso2": "ke", "name": "Kenya", "currency": "INR",
                         "dial_code": "+254"},
                        format="json")
    assert response.status_code == 201, response.json()

    country = Country.objects.get(pk="KE")
    # Added, not opened. Those are two different decisions.
    assert country.is_enabled is False
    assert Corridor.objects.filter(source=country).exists()
    assert Corridor.objects.filter(target=country).exists()
    assert not Corridor.objects.filter(source=country, is_enabled=True).exists()


def test_adding_a_country_refuses_a_currency_the_platform_does_not_know(api, seeded, db):
    from django.utils import timezone

    from nkenzapay.accounts.models import AdminRole, AdminUser, User

    owner = User.objects.create_user(email="owner2@nkenzapay.com",
                                     password="a-long-password-9")
    AdminUser.objects.create(user=owner, role=AdminRole.OWNER,
                             totp_confirmed_at=timezone.now())
    api.force_authenticate(owner)

    response = api.post("/api/v1/admin/countries",
                        {"iso2": "BR", "name": "Brazil", "currency": "BRL"},
                        format="json")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unknown_currency"


def test_the_desk_area_is_closed_to_customers(signed_in, seeded):
    assert signed_in.get("/api/v1/admin/overview").status_code == 403
    assert signed_in.get("/api/v1/admin/users").status_code == 403


def test_the_desk_can_read_the_queue(api, desk, receive_order):
    api.force_authenticate(desk)
    response = api.get("/api/v1/admin/transactions?status=all")
    assert response.status_code == 200
    assert response.json()["counts"]["all"] == 1


def test_a_read_only_admin_cannot_verify(api, receive_order, customer, db):
    from django.utils import timezone

    from nkenzapay.accounts.models import AdminRole, AdminUser, User
    from nkenzapay.transactions import services

    viewer = User.objects.create_user(email="viewer@nkenzapay.com",
                                      password="a-long-password-7")
    AdminUser.objects.create(user=viewer, role=AdminRole.READ_ONLY,
                             totp_confirmed_at=timezone.now())

    services.attach_file(reference=receive_order.reference, user=customer,
                         storage_key="k", original_name="p.png",
                         content_type="image/png", size_bytes=10,
                         is_payment_proof=True)
    services.customer_paid(reference=receive_order.reference, user=customer)

    api.force_authenticate(viewer)
    response = api.post(f"/api/v1/admin/transactions/{receive_order.reference}/verify",
                        {}, format="json")
    assert response.status_code == 403


def test_notification_preferences_lock_transfer_updates(signed_in, customer):
    signed_in.get("/api/v1/notifications/preferences")
    response = signed_in.patch("/api/v1/notifications/preferences",
                               {"channel_group": "transfer_updates", "email": False},
                               format="json")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "locked_group"


def test_chat_messages_are_rejected_when_empty(signed_in, receive_order):
    response = signed_in.post(
        f"/api/v1/transactions/{receive_order.reference}/messages",
        {"body": "   "}, format="json",
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "empty_message"


def test_news_only_lists_published_posts(api, seeded):
    from django.utils import timezone

    from nkenzapay.content.models import NewsPost

    NewsPost.objects.create(slug="draft-piece", title="Draft", is_published=False)
    NewsPost.objects.create(slug="live-piece", title="Live", is_published=True,
                            publish_at=timezone.now())

    slugs = [p["slug"] for p in api.get("/api/v1/news").json()["results"]]
    assert "live-piece" in slugs
    assert "draft-piece" not in slugs


def test_the_profile_logs_an_identity_change(signed_in, customer):
    response = signed_in.patch("/api/v1/me/profile", {"last_name": "Nkenganyi-Doe"},
                               format="json")
    assert response.status_code == 200

    from nkenzapay.accounts.models import ProfileChangeLog

    change = ProfileChangeLog.objects.get(field="last_name")
    assert change.old_value == "Nkenganyi"
    assert change.new_value == "Nkenganyi-Doe"


def test_registration_opens_a_session(api, seeded):
    response = api.post("/api/v1/auth/register", {
        "email": "New.Customer@Example.com",
        "password": "a-decent-password-8",
        "marketing_opt_in": True,
    }, format="json")

    assert response.status_code == 201
    assert response.json()["email"] == "new.customer@example.com"
    assert response.json()["needs_onboarding"] is True

    session = api.get("/api/v1/auth/session").json()
    assert session["user"]["email"] == "new.customer@example.com"


def test_a_duplicate_email_is_refused(api, customer):
    response = api.post("/api/v1/auth/register", {
        "email": "john@example.com", "password": "another-long-password",
    }, format="json")
    assert response.status_code == 400


def test_the_session_endpoint_is_calm_about_anonymity(api, seeded):
    response = api.get("/api/v1/auth/session")
    assert response.status_code == 200
    assert response.json()["user"] is None


def test_the_collection_needs_no_trailing_slash(signed_in, receive_corridor):
    """The front end and Django both redirect trailing slashes, and between
    them they used to bounce a request until the browser gave up. Every API
    path is slash-free, and the slashed form must not redirect."""
    assert signed_in.get("/api/v1/transactions").status_code == 200
    assert signed_in.get("/api/v1/transactions/").status_code == 404


def test_limits_read_as_figures_rather_than_columns(api, send_corridor):
    """Brief section 32: the message says ₹1,000, not 1,000.00 INR."""
    response = api.post("/api/v1/rates/quote", {
        "source": "IN", "target": "CM", "direction": "send",
        "send_amount": "10000",
    }, format="json")

    limits = response.json()["limits"]
    assert limits["minimum_display"] == "₹1,000"
    assert limits["maximum_display"] == "₹5,00,000"


def test_xaf_limits_carry_their_code(api, receive_corridor):
    response = api.post("/api/v1/rates/quote", {
        "source": "CM", "target": "IN", "direction": "receive",
        "send_amount": "100000",
    }, format="json")
    assert response.json()["limits"]["minimum_display"] == "5,000 XAF"
