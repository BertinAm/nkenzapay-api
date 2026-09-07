"""Every desk GET, called once.

A 500 reached production on /admin/payment-methods because nothing in the
suite ever asked for it: the model logic underneath was covered thoroughly and
the endpoint that exposes it was not. These are deliberately thin — they prove
the view builds its response at all, which is the failure that actually
happened.
"""
import pytest
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db


@pytest.fixture
def api():
    return APIClient()


READS = [
    "/api/v1/admin/overview",
    "/api/v1/admin/badges",
    "/api/v1/admin/transactions",
    "/api/v1/admin/messages/inbox",
    "/api/v1/admin/users",
    "/api/v1/admin/settings/rates",
    "/api/v1/admin/settings/fees",
    "/api/v1/admin/settings/limits",
    "/api/v1/admin/settings/company",
    "/api/v1/admin/settings/accounts",
    "/api/v1/admin/payment-methods",
    "/api/v1/admin/countries",
    "/api/v1/admin/news",
    "/api/v1/admin/disputes",
    "/api/v1/admin/notifications",
    "/api/v1/admin/notifications/rules",
    "/api/v1/admin/analytics/website",
    "/api/v1/admin/analytics/users",
    "/api/v1/admin/analytics/transactions",
    "/api/v1/admin/analytics/financial",
    "/api/v1/admin/audit",
    "/api/v1/admin/verifications",
    "/api/v1/admin/2fa",
    "/api/v1/admin/security/overview",
    "/api/v1/admin/security/events",
    "/api/v1/admin/security/blocked",
]


@pytest.mark.parametrize("path", READS)
def test_the_desk_can_read_it(api, owner, seeded, path):
    """An owner, because some of these are settings only an owner may open."""
    api.force_authenticate(owner)
    response = api.get(path)
    assert response.status_code == 200, f"{path}: {response.content[:300]}"


@pytest.mark.parametrize("path", READS)
def test_a_signed_out_visitor_cannot(api, path):
    response = api.get(path)
    assert response.status_code in (401, 403), f"{path} answered {response.status_code}"


def test_a_customer_cannot_reach_the_desk(api, customer, seeded):
    """One check across the whole surface rather than per screen: a customer
    who guesses a desk URL should find a wall, not a gap."""
    api.force_authenticate(customer)
    for path in READS:
        assert api.get(path).status_code == 403, f"{path} let a customer in"
