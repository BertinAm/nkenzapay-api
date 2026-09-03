"""Security behaviour.

These are the tests that matter most on a platform that moves money: a retry
must not pay twice, a probe must be recorded, and a blocked address must stay
blocked.
"""
import uuid
from decimal import Decimal

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from nkenzapay.security import services
from nkenzapay.security.models import BlockedAddress, EventKind, IdempotencyKey, SecurityEvent

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def clear_caches():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def api():
    return APIClient()


# --- idempotency ---------------------------------------------------------


def test_a_repeated_order_creates_one_transfer(api, customer, receive_corridor):
    """The bug this prevents: a double tap producing two transfers, and a
    customer paying for both."""
    from nkenzapay.transactions.models import Transaction

    api.force_authenticate(customer)
    quote = api.post("/api/v1/rates/quote", {
        "source": "CM", "target": "IN", "direction": "receive",
        "send_amount": "100000",
    }, format="json").json()

    key = str(uuid.uuid4())
    body = {"quote_reference": quote["reference"], "collect_method": "mtn_momo"}

    first = api.post("/api/v1/transactions", body, format="json",
                     HTTP_IDEMPOTENCY_KEY=key)
    second = api.post("/api/v1/transactions", body, format="json",
                      HTTP_IDEMPOTENCY_KEY=key)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["reference"] == second.json()["reference"]
    assert Transaction.objects.count() == 1
    assert second["Idempotent-Replay"] == "true"


def test_without_a_key_a_repeat_is_refused_by_the_quote_instead(api, customer,
                                                                receive_corridor):
    """No key means no replay protection, but a spent quote still stops the
    second order. Two layers, so a client that forgets the header is not the
    only thing between a customer and a duplicate."""
    api.force_authenticate(customer)
    quote = api.post("/api/v1/rates/quote", {
        "source": "CM", "target": "IN", "direction": "receive",
        "send_amount": "100000",
    }, format="json").json()
    body = {"quote_reference": quote["reference"], "collect_method": "mtn_momo"}

    assert api.post("/api/v1/transactions", body, format="json").status_code == 201
    second = api.post("/api/v1/transactions", body, format="json")
    assert second.status_code == 400
    assert second.json()["error"]["code"] == "quote_used"


def test_a_key_reused_with_different_content_is_refused(api, customer, receive_corridor):
    api.force_authenticate(customer)
    key = str(uuid.uuid4())

    api.post("/api/v1/auth/register", {"email": "a@example.com",
                                       "password": "a-long-password-11"},
             format="json", HTTP_IDEMPOTENCY_KEY=key)
    second = api.post("/api/v1/auth/register", {"email": "b@example.com",
                                                "password": "a-long-password-12"},
                      format="json", HTTP_IDEMPOTENCY_KEY=key)

    assert second.status_code == 409
    assert second.json()["error"]["code"] == "idempotency_key_reused"


def test_signup_is_idempotent(api, seeded):
    from nkenzapay.accounts.models import User

    key = str(uuid.uuid4())
    body = {"email": "retry@example.com", "password": "a-long-password-13"}

    first = api.post("/api/v1/auth/register", body, format="json",
                     HTTP_IDEMPOTENCY_KEY=key)
    second = api.post("/api/v1/auth/register", body, format="json",
                      HTTP_IDEMPOTENCY_KEY=key)

    assert first.status_code == 201
    assert second.status_code == 201
    assert User.objects.filter(email="retry@example.com").count() == 1


def test_a_signup_retry_finds_the_key_the_first_attempt_stored(api, seeded):
    """The scope changes underneath this one, which is what made it fail.

    Sign-up signs the new account in, so the first attempt arrives anonymous
    and is stored against the address, while the retry arrives holding a
    session and looks for a key belonging to the account. Matching only the
    current scope missed the stored record and made a second account - the
    exact failure the feature exists to prevent.
    """
    from nkenzapay.accounts.models import User

    key = str(uuid.uuid4())
    body = {"email": "scope@example.com", "password": "a-long-password-13"}

    api.post("/api/v1/auth/register", body, format="json", HTTP_IDEMPOTENCY_KEY=key)

    stored = IdempotencyKey.objects.get(key=key)
    assert stored.scope.startswith("ip:"), "the first attempt was anonymous"

    user = User.objects.get(email="scope@example.com")
    api.force_authenticate(user)
    retry = api.post("/api/v1/auth/register", body, format="json",
                     HTTP_IDEMPOTENCY_KEY=key)

    assert retry.status_code == 201
    assert retry["Idempotent-Replay"] == "true"
    assert User.objects.filter(email="scope@example.com").count() == 1
    assert IdempotencyKey.objects.filter(key=key).count() == 1


def test_one_customers_key_cannot_replay_anothers(api, customer, seeded, db):
    """Keys are scoped per account, so guessing one gets you nothing."""
    from nkenzapay.accounts.models import User

    other = User.objects.create_user(email="other@example.com",
                                     password="a-long-password-14")
    key = "shared-key-value"

    api.force_authenticate(customer)
    api.post("/api/v1/notifications/read", {}, format="json", HTTP_IDEMPOTENCY_KEY=key)

    api.force_authenticate(other)
    response = api.post("/api/v1/notifications/read", {}, format="json",
                        HTTP_IDEMPOTENCY_KEY=key)
    assert response.status_code == 200

    assert IdempotencyKey.objects.filter(key=key).count() <= 2


# --- probes and scanning --------------------------------------------------


@pytest.mark.parametrize("target", [
    "/api/v1/news?q=1%20UNION%20SELECT%20password%20FROM%20users",
    "/api/v1/news?q=%3Cscript%3Ealert(1)%3C/script%3E",
    "/api/v1/news?q=1%20OR%201=1",
    "/api/v1/news?q=${jndi:ldap://x}",
])
def test_injection_probes_are_refused_and_recorded(api, seeded, target):
    response = api.get(target)
    assert response.status_code == 403
    assert SecurityEvent.objects.filter(kind=EventKind.INJECTION_PROBE).exists()


@pytest.mark.parametrize("target", [
    # Plain, as a script that has not thought about it would send.
    "/api/v1/news?q=1 UNION SELECT x",
    # Percent-encoded once, which is what actually arrives on the wire. The
    # first version of this check read the raw path and saw none of these.
    "/api/v1/news?q=1%20UNION%20SELECT%20x",
    # Encoded twice. The classic way past a filter that decodes once and then
    # hands the still-encoded string to something that decodes again.
    "/api/v1/news?q=1%2520UNION%2520SELECT%2520x",
    # Spaces as plus signs, which is the other spelling of the same request.
    "/api/v1/news?q=1+UNION+SELECT+x",
    "/api/v1/news?q=%3Cscript%3E",
    "/api/v1/news?q=%253Cscript%253E",
])
def test_a_probe_is_caught_however_it_is_encoded(api, seeded, target):
    """Encoding is not evasion.

    This is the bug that made the whole tripwire decorative: the check ran on
    the URL as it arrived, so every probe went past it, because probes arrive
    percent-encoded. The path is decoded twice before it is read.
    """
    assert api.get(target).status_code == 403
    assert SecurityEvent.objects.filter(kind=EventKind.INJECTION_PROBE).exists()


def test_path_traversal_is_refused(api, seeded):
    response = api.get("/api/v1/legal/../../etc/passwd")
    assert response.status_code == 403
    assert SecurityEvent.objects.filter(kind=EventKind.TRAVERSAL_PROBE).exists()


@pytest.mark.parametrize("target", [
    "/api/v1/legal/%2e%2e%2f%2e%2e%2fetc/passwd",
    "/api/v1/legal/%252e%252e%252fetc/passwd",
])
def test_encoded_traversal_is_refused(api, seeded, target):
    assert api.get(target).status_code == 403
    assert SecurityEvent.objects.filter(kind=EventKind.TRAVERSAL_PROBE).exists()


def test_ordinary_content_is_not_mistaken_for_an_attack(api, seeded):
    """The tripwire has to stay quiet on real traffic, or nobody reads it."""
    for target in [
        "/api/v1/news?q=select%20a%20payment%20method",
        "/api/v1/news?q=Ivoire",
        "/api/v1/news?q=rates%20and%20fees",
    ]:
        assert api.get(target).status_code == 200
    assert not SecurityEvent.objects.filter(
        kind__in=[EventKind.INJECTION_PROBE, EventKind.TRAVERSAL_PROBE]
    ).exists()


def test_scanning_for_other_software_is_recorded(api, seeded):
    api.get("/wp-login.php")
    api.get("/.env")
    assert SecurityEvent.objects.filter(kind=EventKind.SCANNER).count() == 2


# --- blocking -------------------------------------------------------------


def test_a_blocked_address_is_refused(api, seeded):
    services.block("45.155.205.9", reason="testing")
    response = api.get("/api/v1/geo/countries", REMOTE_ADDR="45.155.205.9")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "refused"


def test_an_expired_block_lets_the_address_back_in(api, seeded):
    from datetime import timedelta

    from django.utils import timezone

    BlockedAddress.objects.create(
        ip="45.155.205.10", reason="expired",
        expires_at=timezone.now() - timedelta(hours=1),
    )
    assert api.get("/api/v1/geo/countries", REMOTE_ADDR="45.155.205.10").status_code == 200


def test_repeated_probes_get_the_address_blocked(api, seeded):
    for _ in range(4):
        api.get("/api/v1/news?q=1%20UNION%20SELECT%20x", REMOTE_ADDR="45.155.205.11")
    assert BlockedAddress.objects.filter(ip="45.155.205.11").exists()


def test_a_forged_forwarding_header_cannot_change_the_address(api, seeded, settings):
    """With no trusted proxy configured, a client cannot claim to be someone
    else and slip past their own block."""
    settings.TRUSTED_IP_HEADERS = []
    services.block("45.155.205.12", reason="testing")

    response = api.get(
        "/api/v1/geo/countries",
        REMOTE_ADDR="45.155.205.12",
        HTTP_X_FORWARDED_FOR="8.8.8.8",
    )
    assert response.status_code == 403


def test_the_proxy_can_vouch_for_a_callers_address(api, seeded, settings):
    """The front end proxies /api to this service, so without a way to pass the
    real address through, every customer arrives as the Worker and shares one
    rate limit."""
    settings.PROXY_SHARED_SECRET = "a" * 40
    settings.TRUSTED_IP_HEADERS = ["HTTP_CF_CONNECTING_IP"]
    services.block("45.155.205.13", reason="testing")

    response = api.get(
        "/api/v1/geo/countries",
        # Cloudflare sees the Worker on this hop, not the customer.
        HTTP_CF_CONNECTING_IP="2a06:98c0:3600::103",
        HTTP_X_CLIENT_IP="45.155.205.13",
        HTTP_X_PROXY_TOKEN="a" * 40,
    )
    assert response.status_code == 403


def test_a_forged_client_ip_header_is_ignored_without_the_secret(api, seeded, settings):
    """The whole point of the token. Anyone can set X-Client-IP by hand, and if
    that were enough, a blocked address would only have to claim to be another
    one to be let back in."""
    settings.PROXY_SHARED_SECRET = "a" * 40
    settings.TRUSTED_IP_HEADERS = ["HTTP_CF_CONNECTING_IP"]
    services.block("45.155.205.14", reason="testing")

    blocked = api.get(
        "/api/v1/geo/countries",
        HTTP_CF_CONNECTING_IP="45.155.205.14",
        HTTP_X_CLIENT_IP="8.8.8.8",
        HTTP_X_PROXY_TOKEN="not-the-secret",
    )
    assert blocked.status_code == 403

    # And the same request with no token at all.
    still_blocked = api.get(
        "/api/v1/geo/countries",
        HTTP_CF_CONNECTING_IP="45.155.205.14",
        HTTP_X_CLIENT_IP="8.8.8.8",
    )
    assert still_blocked.status_code == 403


def test_the_client_ip_header_is_dead_until_a_secret_is_configured(api, seeded, settings):
    """An installation with no proxy in front of it must not honour the header
    at all, or it has handed every caller a way to pick their own address."""
    settings.PROXY_SHARED_SECRET = ""
    settings.TRUSTED_IP_HEADERS = ["HTTP_CF_CONNECTING_IP"]
    services.block("45.155.205.15", reason="testing")

    response = api.get(
        "/api/v1/geo/countries",
        HTTP_CF_CONNECTING_IP="45.155.205.15",
        HTTP_X_CLIENT_IP="8.8.8.8",
        HTTP_X_PROXY_TOKEN="",
    )
    assert response.status_code == 403


# --- failed sign-ins ------------------------------------------------------


def test_a_failed_sign_in_is_recorded(api, customer):
    api.post("/api/v1/auth/login",
             {"email": "john@example.com", "password": "wrong-password"},
             format="json")
    event = SecurityEvent.objects.filter(kind=EventKind.LOGIN_FAILED).first()
    assert event is not None
    assert event.identifier == "john@example.com"


def test_the_reply_does_not_reveal_whether_the_account_exists(api, customer):
    known = api.post("/api/v1/auth/login",
                     {"email": "john@example.com", "password": "wrong-password"},
                     format="json")
    unknown = api.post("/api/v1/auth/login",
                       {"email": "nobody@example.com", "password": "wrong-password"},
                       format="json")

    assert known.status_code == unknown.status_code
    assert known.json()["error"]["message"] == unknown.json()["error"]["message"]

    # The desk still gets the distinction, in the event rather than the reply.
    events = SecurityEvent.objects.filter(kind=EventKind.LOGIN_FAILED)
    assert {e.detail["account_exists"] for e in events} == {True, False}


def test_repeated_failures_lock_the_account(api, customer):
    for _ in range(6):
        api.post("/api/v1/auth/login",
                 {"email": "john@example.com", "password": "wrong-password"},
                 format="json")

    response = api.post("/api/v1/auth/login",
                        {"email": "john@example.com",
                         "password": "nkenza-demo-2026"},
                        format="json")
    assert response.json()["error"]["code"] == "locked_out"
    assert SecurityEvent.objects.filter(kind=EventKind.LOGIN_LOCKED).exists()


def test_password_reset_says_the_same_thing_either_way(api, customer):
    known = api.post("/api/v1/auth/password/reset",
                     {"email": "john@example.com"}, format="json")
    unknown = api.post("/api/v1/auth/password/reset",
                       {"email": "nobody@example.com"}, format="json")
    assert known.json() == unknown.json()


# --- the desk's view ------------------------------------------------------


def test_the_security_screen_is_closed_to_customers(api, customer, seeded):
    api.force_authenticate(customer)
    assert api.get("/api/v1/admin/security/overview").status_code == 403
    assert api.get("/api/v1/admin/security/events").status_code == 403


def test_the_desk_sees_events(api, desk, seeded):
    services.record(EventKind.SCANNER, summary="probed /wp-login.php")

    api.force_authenticate(desk)
    overview = api.get("/api/v1/admin/security/overview")
    assert overview.status_code == 200
    assert overview.json()["totals"]["events"] >= 1

    events = api.get("/api/v1/admin/security/events")
    assert events.status_code == 200
    assert events.json()["count"] >= 1


def test_only_an_owner_can_block_an_address(api, desk, seeded):
    api.force_authenticate(desk)   # payments role, not owner
    response = api.post("/api/v1/admin/security/block",
                        {"ip": "45.155.205.50", "reason": "testing"}, format="json")
    assert response.status_code == 403


def test_a_probe_payload_is_never_stored_whole(api, seeded):
    """The desk reads these in a browser. A stored payload is trimmed and shown
    as text, never rendered."""
    api.get("/api/v1/news?q=" + "%3Cscript%3E" + "A" * 4000)
    event = SecurityEvent.objects.filter(kind=EventKind.INJECTION_PROBE).first()
    assert event is not None
    for value in event.detail.values():
        assert len(str(value)) <= 501


def test_loopback_is_never_auto_blocked(api, seeded):
    """Behind a proxy with no trusted header set, every request looks like it
    comes from 127.0.0.1. Auto-blocking that would take the site down for
    everyone, so the event is recorded and the block is not applied."""
    for _ in range(6):
        api.get("/api/v1/news?q=1%20UNION%20SELECT%20x", REMOTE_ADDR="127.0.0.1")

    assert SecurityEvent.objects.filter(kind=EventKind.INJECTION_PROBE).count() == 6
    assert not BlockedAddress.objects.filter(ip="127.0.0.1").exists()


def test_a_private_range_address_is_never_auto_blocked(api, seeded):
    for _ in range(6):
        api.get("/api/v1/news?q=1%20UNION%20SELECT%20x", REMOTE_ADDR="10.0.0.5")
    assert not BlockedAddress.objects.filter(ip="10.0.0.5").exists()


def test_a_person_can_still_block_an_internal_address(seeded):
    """The automatic rule is a safety catch, not a policy. A human who means it
    can still block one."""
    services.block("127.0.0.1", reason="deliberate", actor=None)
    assert BlockedAddress.objects.filter(ip="127.0.0.1").exists()


def test_the_lockout_fails_open_when_the_cache_is_broken(api, customer, monkeypatch):
    """A broken cache must not lock every customer out of their own money.

    This is the failure that took every request down in development: the cache
    table did not exist, and the lockout check let the exception escape.
    """
    from nkenzapay.accounts import views

    def explode(*args, **kwargs):
        raise RuntimeError("cache is down")

    monkeypatch.setattr(views.cache, "get", explode)
    monkeypatch.setattr(views.cache, "set", explode)
    monkeypatch.setattr(views.cache, "delete", explode)

    customer.set_password("a-known-password-42")
    customer.save()

    response = api.post("/api/v1/auth/login",
                        {"email": "john@example.com",
                         "password": "a-known-password-42"},
                        format="json")
    assert response.status_code == 200


def test_blocking_falls_back_to_the_database_when_the_cache_is_broken(
    api, seeded, monkeypatch
):
    """The cache is an optimisation. A block still holds without it."""
    from nkenzapay.security import services as security_services

    services.block("45.155.205.20", reason="testing")

    def explode(*args, **kwargs):
        raise RuntimeError("cache is down")

    monkeypatch.setattr(security_services.cache, "get", explode)
    monkeypatch.setattr(security_services.cache, "set", explode)

    response = api.get("/api/v1/geo/countries", REMOTE_ADDR="45.155.205.20")
    assert response.status_code == 403


def test_a_broken_cache_does_not_refuse_everybody(api, seeded, monkeypatch):
    """The other half of the same rule.

    A cache fault must not read as "blocked" for callers who are not. This is
    the shape of the outage that took every request down: one dependency
    failing, and every request failing with it.
    """
    from nkenzapay.security import services as security_services

    def explode(*args, **kwargs):
        raise RuntimeError("cache is down")

    monkeypatch.setattr(security_services.cache, "get", explode)
    monkeypatch.setattr(security_services.cache, "set", explode)

    assert api.get("/api/v1/geo/countries",
                   REMOTE_ADDR="45.155.205.21").status_code == 200


def test_rate_limiting_lets_the_request_through_when_the_cache_is_broken(
    api, seeded, monkeypatch
):
    """DRF's own throttle lets a cache error escape, which turns a cache
    outage into a 500 on every rate-limited endpoint. Losing the limit for the
    length of the outage is the smaller problem."""
    import rest_framework.throttling as drf_throttling

    class BrokenCache:
        def get(self, *args, **kwargs):
            raise RuntimeError("cache is down")

        def set(self, *args, **kwargs):
            raise RuntimeError("cache is down")

    monkeypatch.setattr(drf_throttling.SimpleRateThrottle, "cache", BrokenCache())

    response = api.get("/api/v1/geo/countries")
    assert response.status_code == 200
