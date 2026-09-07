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


def test_a_password_reset_email_carries_a_working_link(api, customer, mailoutbox, settings):
    """The bug this guards against shipped once already: the endpoint answered
    {"sent": true}, an email arrived, and it contained no link at all — so a
    customer who forgot their password was locked out for good."""
    settings.SITE_URL = "https://nkenzapay.com"

    api.post("/api/v1/auth/password/reset", {"email": customer.email},
             format="json")

    assert len(mailoutbox) == 1
    body = mailoutbox[0].body
    assert "https://nkenzapay.com/reset-password?token=" in body

    # And the link works.
    token = body.split("token=")[1].split()[0]
    response = api.post("/api/v1/auth/password/reset/confirm",
                        {"token": token, "new_password": "a-brand-new-password-1"},
                        format="json")
    assert response.status_code == 200, response.json()


def test_the_email_goes_out_as_text_and_html(api, customer, mailoutbox, settings):
    """Both parts carry the same link. A client that refuses HTML, or someone
    who reads mail as plain text, gets the message rather than an apology."""
    settings.SITE_URL = "https://nkenzapay.com"
    api.post("/api/v1/auth/password/reset", {"email": customer.email},
             format="json")

    message = mailoutbox[0]
    html = next(
        content for content, mimetype in message.alternatives
        if mimetype == "text/html"
    )

    assert "https://nkenzapay.com/reset-password?token=" in message.body
    assert "https://nkenzapay.com/reset-password?token=" in html
    assert "Set a new password" in html
    # No stylesheet, no webfont, no image: an email that only looks right once
    # images are allowed looks broken the first time most people see it.
    assert "<link" not in html
    assert "<img" not in html


def test_the_reset_token_never_reaches_the_stored_notification(api, customer, mailoutbox):
    """A live reset link sitting in a row the bell renders is a second way into
    the account, readable by anyone holding the session."""
    from nkenzapay.notifications.models import Notification

    api.post("/api/v1/auth/password/reset", {"email": customer.email},
             format="json")

    token = mailoutbox[0].body.split("token=")[1].split()[0]
    stored = Notification.objects.filter(event="account.password_reset").first()
    assert stored is not None
    assert token not in stored.body


def test_a_reset_for_an_unknown_address_sends_nothing(api, db, mailoutbox):
    """Same answer either way, so nobody can harvest which addresses exist —
    but no email leaves the building."""
    response = api.post("/api/v1/auth/password/reset",
                        {"email": "nobody@example.com"}, format="json")
    assert response.json() == {"sent": True}
    assert mailoutbox == []


def test_a_desk_account_can_enrol_in_two_factor(api, desk, db):
    """Every money-moving path checks totp_confirmed_at, and until this existed
    nothing could set it: a seeded owner could read the whole desk and verify
    nothing."""
    from django_otp.oath import TOTP
    from django_otp.plugins.otp_totp.models import TOTPDevice

    from nkenzapay.accounts.models import AdminUser, User

    AdminUser.objects.filter(user=desk).update(totp_confirmed_at=None)
    # Re-read the user: the fixture's instance still holds the profile it was
    # created with, and the view reads it off the authenticated user.
    desk = User.objects.get(pk=desk.pk)

    api.force_authenticate(desk)
    assert api.get("/api/v1/admin/2fa").json()["enrolled"] is False

    started = api.post("/api/v1/admin/2fa/setup", {}, format="json")
    assert started.status_code == 200
    assert started.json()["otpauth_url"].startswith("otpauth://totp/")
    assert "<svg" in started.json()["qr_svg"]

    device = TOTPDevice.objects.get(user=desk, confirmed=False)
    totp = TOTP(device.bin_key, device.step, device.t0, device.digits)
    code = str(totp.token()).zfill(device.digits)

    done = api.post("/api/v1/admin/2fa/confirm", {"code": code}, format="json")
    assert done.status_code == 200, done.json()

    assert AdminUser.objects.get(user=desk).totp_confirmed_at is not None
    assert TOTPDevice.objects.get(user=desk).confirmed is True


def test_a_wrong_code_does_not_enrol(api, desk, db):
    from nkenzapay.accounts.models import AdminUser, User

    AdminUser.objects.filter(user=desk).update(totp_confirmed_at=None)
    desk = User.objects.get(pk=desk.pk)

    api.force_authenticate(desk)
    assert api.post("/api/v1/admin/2fa/setup", {}, format="json").status_code == 200

    response = api.post("/api/v1/admin/2fa/confirm", {"code": "000000"},
                        format="json")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_code"

    assert AdminUser.objects.get(user=desk).totp_confirmed_at is None


def test_enrolment_is_refused_once_it_is_done(api, desk, db):
    """Someone holding a stolen session must not be able to re-bind two-factor
    to their own phone and lock the real owner out."""
    api.force_authenticate(desk)
    response = api.post("/api/v1/admin/2fa/setup", {}, format="json")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "already_enrolled"


def test_an_unapproved_account_cannot_open_a_transfer(api, unverified_customer,
                                                     receive_corridor, configured_methods):
    """Money leaves the platform against a name, so the desk has to have seen
    the document that name came from before the first transfer, not after."""
    from decimal import Decimal

    from nkenzapay.pricing.engine import build_quote, persist_quote
    from nkenzapay.transactions import services

    result = build_quote(corridor=receive_corridor, direction="receive",
                         send_amount=Decimal("100000"), user=unverified_customer)
    quote = persist_quote(result, user=unverified_customer)

    from nkenzapay.common.exceptions import DomainError
    from nkenzapay.payments.models import PaymentMethod

    method = PaymentMethod.objects.get(slug="mtn_momo")
    with pytest.raises(DomainError) as raised:
        services.create_transaction(
            user=unverified_customer, quote=quote, collect_method=method
        )

    assert raised.value.code == "not_verified"
    # Named steps, so the app can send them to the right one rather than
    # showing the sentence and stopping.
    assert "id_document" in raised.value.detail["missing"]


def test_submitting_a_document_puts_the_account_in_the_queue(api, unverified_customer,
                                                             settings, tmp_path):
    from nkenzapay.accounts.models import Profile

    profile = unverified_customer.profile
    profile.id_document_key = "identity/9/passport.jpg"
    profile.id_document_type = "passport"
    profile.verification_state = Profile.PENDING
    profile.save()

    api.force_authenticate(unverified_customer)
    body = api.get("/api/v1/me/profile").json()
    assert body["verification_state"] == "pending"
    assert body["is_verified"] is False
    assert body["has_id_document"] is True


def test_the_desk_approves_and_the_customer_is_told(api, desk, unverified_customer,
                                                   mailoutbox):
    from nkenzapay.accounts.models import Profile

    profile = unverified_customer.profile
    profile.id_document_key = "identity/9/passport.jpg"
    profile.id_document_type = "passport"
    profile.verification_state = Profile.PENDING
    profile.save()

    api.force_authenticate(desk)
    response = api.post(f"/api/v1/admin/verifications/{profile.pk}/approve",
                        {}, format="json")
    assert response.status_code == 200, response.json()

    profile.refresh_from_db()
    assert profile.verification_state == Profile.APPROVED
    assert profile.verified_by_id == desk.pk
    assert any("approved" in message.subject.lower() for message in mailoutbox)


def test_rejecting_without_a_reason_is_refused(api, desk, unverified_customer):
    """The customer is about to be asked for another document and has to know
    what was wrong with the first one."""
    from nkenzapay.accounts.models import Profile

    profile = unverified_customer.profile
    profile.id_document_key = "identity/9/passport.jpg"
    profile.verification_state = Profile.PENDING
    profile.save()

    api.force_authenticate(desk)
    response = api.post(f"/api/v1/admin/verifications/{profile.pk}/reject",
                        {"note": "   "}, format="json")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "reason_required"


def test_reminders_follow_the_schedule_and_then_stop(unverified_customer, mailoutbox):
    """A day, three days, then weekly, then silence. Somebody who has ignored
    four emails is not reading the fifth."""
    from datetime import timedelta

    from django.core.management import call_command
    from django.utils import timezone

    profile = unverified_customer.profile

    # Too soon on the day they signed up.
    call_command("send_verification_reminders")
    profile.refresh_from_db()
    assert profile.reminders_sent == 0

    def wind_back(hours):
        stamp = timezone.now() - timedelta(hours=hours)
        type(profile).objects.filter(pk=profile.pk).update(
            created_at=stamp, last_reminder_at=None if profile.reminders_sent == 0 else stamp
        )
        profile.refresh_from_db()

    for expected in (1, 2, 3, 4):
        wind_back(24 * 30)
        call_command("send_verification_reminders")
        profile.refresh_from_db()
        assert profile.reminders_sent == expected, expected

    # Four is the end of it, however long anybody waits.
    wind_back(24 * 365)
    call_command("send_verification_reminders")
    profile.refresh_from_db()
    assert profile.reminders_sent == 4
    assert len(mailoutbox) == 4


def test_an_approved_account_is_never_reminded(customer, mailoutbox):
    from datetime import timedelta

    from django.core.management import call_command
    from django.utils import timezone

    type(customer.profile).objects.filter(pk=customer.profile.pk).update(
        created_at=timezone.now() - timedelta(days=90)
    )
    call_command("send_verification_reminders")

    customer.profile.refresh_from_db()
    assert customer.profile.reminders_sent == 0
    assert mailoutbox == []


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
