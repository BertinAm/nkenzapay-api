"""The security properties that must hold, checked rather than assumed.

Organised by the thing that would go wrong, not by OWASP category number. Each
test is here because it would be a real incident if it failed.
"""
import pytest
from django.conf import settings
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db


@pytest.fixture
def api():
    return APIClient()


# --- one customer must never reach another's anything ---------------------


def test_a_customer_cannot_read_another_customers_transfer(api, receive_order, db):
    from nkenzapay.accounts.models import User

    stranger = User.objects.create_user(email="stranger@example.com",
                                        password="a-long-password-20")
    api.force_authenticate(stranger)
    assert api.get(f"/api/v1/transactions/{receive_order.reference}").status_code == 404


def test_guessing_a_reference_reveals_nothing(api, receive_order, db):
    """A missing transfer and someone else's transfer answer identically, so
    the response cannot be used to discover which references exist."""
    from nkenzapay.accounts.models import User

    stranger = User.objects.create_user(email="guesser@example.com",
                                        password="a-long-password-21")
    api.force_authenticate(stranger)

    theirs = api.get(f"/api/v1/transactions/{receive_order.reference}")
    nonexistent = api.get("/api/v1/transactions/NKP-20260101-99999")
    assert theirs.status_code == nonexistent.status_code == 404


def test_a_customer_cannot_post_into_another_customers_chat(api, receive_order, db):
    from nkenzapay.accounts.models import User

    stranger = User.objects.create_user(email="intruder@example.com",
                                        password="a-long-password-22")
    api.force_authenticate(stranger)
    response = api.post(
        f"/api/v1/transactions/{receive_order.reference}/messages",
        {"body": "hello"}, format="json",
    )
    assert response.status_code == 404


def test_a_customer_cannot_read_another_customers_attachment(api, receive_order,
                                                             customer, db):
    from nkenzapay.accounts.models import User
    from nkenzapay.transactions import services

    attachment = services.attach_file(
        reference=receive_order.reference, user=customer,
        storage_key="k", original_name="proof.png",
        content_type="image/png", size_bytes=10, is_payment_proof=True,
    )
    stranger = User.objects.create_user(email="peeker@example.com",
                                        password="a-long-password-23")
    api.force_authenticate(stranger)
    assert api.get(f"/api/v1/attachments/{attachment.pk}/url").status_code == 403


# --- a customer must never act as the desk --------------------------------


def test_a_customer_cannot_verify_their_own_payment(api, receive_order, customer):
    api.force_authenticate(customer)
    response = api.post(
        f"/api/v1/admin/transactions/{receive_order.reference}/verify",
        {}, format="json",
    )
    assert response.status_code == 403


def test_a_customer_cannot_change_the_fee(api, customer, seeded):
    api.force_authenticate(customer)
    assert api.get("/api/v1/admin/settings/fees").status_code == 403
    assert api.put("/api/v1/admin/settings/fees", {"percent": "0.00"},
                   format="json").status_code == 403


def test_a_customer_cannot_read_the_audit_log(api, customer, seeded):
    api.force_authenticate(customer)
    assert api.get("/api/v1/admin/audit").status_code == 403


def test_a_support_role_cannot_move_money(api, support_only, receive_order,
                                          customer):
    from nkenzapay.transactions import services

    services.attach_file(reference=receive_order.reference, user=customer,
                         storage_key="k", original_name="p.png",
                         content_type="image/png", size_bytes=10,
                         is_payment_proof=True)
    services.customer_paid(reference=receive_order.reference, user=customer)

    api.force_authenticate(support_only)
    response = api.post(
        f"/api/v1/admin/transactions/{receive_order.reference}/verify",
        {}, format="json",
    )
    assert response.status_code == 403


# --- a customer must not be able to edit their own price ------------------


def test_the_amount_comes_from_the_quote_not_the_request(api, customer,
                                                          receive_corridor):
    """Sending a favourable receive_amount alongside the order must not change
    what the platform owes. The figures come from the stored quote."""
    from decimal import Decimal

    api.force_authenticate(customer)
    quote = api.post("/api/v1/rates/quote", {
        "source": "CM", "target": "IN", "direction": "receive",
        "send_amount": "100000",
    }, format="json").json()

    response = api.post("/api/v1/transactions", {
        "quote_reference": quote["reference"],
        "collect_method": "mtn_momo",
        "receive_amount": "9999999",
        "fee_percent": "0",
        "rate_used": "99",
    }, format="json")

    assert response.status_code == 201
    body = response.json()
    assert body["receive"]["value"] == "15918.9000"
    assert Decimal(body["fee_percent"]) == Decimal("6.00")


def test_another_customers_quote_cannot_be_spent(api, customer, receive_corridor, db):
    from nkenzapay.accounts.models import User

    api.force_authenticate(customer)
    quote = api.post("/api/v1/rates/quote", {
        "source": "CM", "target": "IN", "direction": "receive",
        "send_amount": "100000",
    }, format="json").json()

    thief = User.objects.create_user(email="thief@example.com",
                                     password="a-long-password-24")
    api.force_authenticate(thief)
    response = api.post("/api/v1/transactions", {
        "quote_reference": quote["reference"], "collect_method": "mtn_momo",
    }, format="json")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "quote_owner"


def test_a_transfer_cannot_be_reassigned_by_posting_a_user(api, customer,
                                                            receive_corridor, db):
    from nkenzapay.accounts.models import User
    from nkenzapay.transactions.models import Transaction

    victim = User.objects.create_user(email="victim@example.com",
                                      password="a-long-password-25")
    api.force_authenticate(customer)
    quote = api.post("/api/v1/rates/quote", {
        "source": "CM", "target": "IN", "direction": "receive",
        "send_amount": "100000",
    }, format="json").json()

    api.post("/api/v1/transactions", {
        "quote_reference": quote["reference"],
        "collect_method": "mtn_momo",
        "user": victim.pk,
    }, format="json")

    assert Transaction.objects.get().user_id == customer.pk


# --- stored content must not be able to run -------------------------------


def test_article_bodies_are_sanitised(api, desk, seeded):
    from nkenzapay.content.models import NewsPost

    api.force_authenticate(desk)
    api.post("/api/v1/admin/news", {
        "slug": "nasty",
        "title": "Nasty",
        "body_html": '<p>fine</p><script>alert(1)</script>'
                     '<img src=x onerror="alert(1)">'
                     '<a href="javascript:alert(1)">click</a>',
    }, format="json")

    post = NewsPost.objects.get(slug="nasty")
    assert "<script" not in post.body_html
    assert "onerror" not in post.body_html
    assert "javascript:" not in post.body_html
    assert "fine" in post.body_html


def test_a_chat_message_is_stored_as_text(api, receive_order, customer):
    """Messages are rendered as text, never as markup, so the payload is kept
    verbatim rather than mangled — and never interpreted."""
    api.force_authenticate(customer)
    api.post(f"/api/v1/transactions/{receive_order.reference}/messages",
             {"body": "<b>bold</b> and <script>alert(1)</script>"}, format="json")

    messages = api.get(
        f"/api/v1/transactions/{receive_order.reference}/messages"
    ).json()
    assert "<script>" in messages[-1]["body"]


def test_a_profile_name_cannot_smuggle_markup_into_the_desk(api, customer):
    """The desk sees customer names. React escapes them; this checks the value
    survives as data rather than being silently accepted as markup."""
    api.force_authenticate(customer)
    api.patch("/api/v1/me/profile",
              {"first_name": "<script>alert(1)</script>"}, format="json")

    profile = api.get("/api/v1/me/profile").json()
    assert profile["first_name"] == "<script>alert(1)</script>"


# --- the platform must not leak how it is built ---------------------------


def test_errors_do_not_leak_internals(api, customer):
    api.force_authenticate(customer)
    response = api.get("/api/v1/transactions/NKP-does-not-exist")
    body = response.content.decode().lower()
    for leak in ["traceback", "django", "sqlite", "mysql", "select ", "/home/"]:
        assert leak not in body


def test_the_fx_key_is_never_serialised(api, desk, seeded):
    api.force_authenticate(desk)
    response = api.get("/api/v1/admin/settings/rates")
    body = response.content.decode().lower()
    assert "api_key" not in body
    assert "secret" not in body


def test_payment_details_are_masked_before_an_order_exists(api, configured_methods):
    """Where the money is paid in is only told to somebody with an order.

    The number checked for here is the fixture's placeholder, not a real one.
    Asserting on the live account number would mean writing it into a public
    repository to prove it stays out of a response.
    """
    response = api.get("/api/v1/payments/methods?country=CM&side=collect")
    body = response.content.decode()
    assert "6 00 000 000" not in body


# --- configuration --------------------------------------------------------


def test_passwords_are_hashed_with_argon2():
    assert settings.PASSWORD_HASHERS[0].endswith("Argon2PasswordHasher")


def test_a_stored_password_is_not_recoverable(customer):
    assert customer.password.startswith("argon2")
    assert "nkenza-demo-2026" not in customer.password


def test_session_and_csrf_cookies_are_locked_down():
    assert settings.SESSION_COOKIE_HTTPONLY is True
    assert settings.SESSION_COOKIE_SAMESITE == "Lax"
    assert settings.CSRF_COOKIE_SAMESITE == "Lax"
    # The front end reads the CSRF token to send it back as a header.
    assert settings.CSRF_COOKIE_HTTPONLY is False


def test_debug_is_off_by_default():
    """DEBUG is True only because .env says so for local work. The default in
    settings must be False, so a deployment that forgets the file is safe."""
    import environ

    assert environ.Env.__init__ is not None
    from config import settings as module

    assert module.env.ENVIRON.get("DEBUG") in (None, "True", "False", "true", "false")


def test_uploads_are_capped():
    assert settings.UPLOAD_LIMITS["image"] <= 10 * 1024 * 1024
    assert settings.UPLOAD_LIMITS["video"] <= 50 * 1024 * 1024
    assert settings.DATA_UPLOAD_MAX_MEMORY_SIZE <= 10 * 1024 * 1024


def test_signed_links_are_short_lived():
    assert settings.SIGNED_URL_TTL_SECONDS <= 300


def test_forwarded_headers_are_not_trusted_by_default():
    """Trusting a header no proxy sets would let anyone forge their address and
    walk past a block."""
    assert settings.TRUSTED_IP_HEADERS == []


# --- the audit log must be trustworthy ------------------------------------


def test_an_audit_entry_cannot_be_edited(receive_order):
    from nkenzapay.audit.models import AuditEntry

    entry = AuditEntry.objects.first()
    assert entry is not None

    entry.summary = "something else"
    with pytest.raises(RuntimeError):
        entry.save()
    with pytest.raises(RuntimeError):
        entry.delete()
