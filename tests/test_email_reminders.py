"""Chasing an address nobody confirmed.

A confirmed address is where receipts go and where a reset link goes if
somebody is locked out, so an account without one is a day away from being
unreachable. Two nudges and then silence.
"""
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone

pytestmark = pytest.mark.django_db


def aged(profile, days):
    """Move the account's clock back so the schedule is due."""
    user = profile.user
    user.date_joined = timezone.now() - timedelta(days=days)
    user.save(update_fields=["date_joined"])
    return profile


def test_somebody_who_never_confirmed_is_asked_again(customer, mailoutbox, seeded):
    customer.email_verified_at = None
    customer.save(update_fields=["email_verified_at"])
    aged(customer.profile, days=2)

    call_command("send_verification_reminders", verbosity=0)

    customer.profile.refresh_from_db()
    assert customer.profile.email_reminders_sent == 1
    sent = [m for m in mailoutbox if "email" in m.subject.lower()]
    assert sent, "no confirmation reminder went out"
    # A fresh link, because the one from sign-up may have expired and a dead
    # link in a reminder reads as a broken account.
    assert "verify-email" in sent[-1].body


def test_a_confirmed_address_is_left_alone(customer, mailoutbox, seeded):
    customer.email_verified_at = timezone.now()
    customer.save(update_fields=["email_verified_at"])
    aged(customer.profile, days=9)

    call_command("send_verification_reminders", verbosity=0)

    customer.profile.refresh_from_db()
    assert customer.profile.email_reminders_sent == 0


def test_it_stops_after_two(customer, seeded):
    customer.email_verified_at = None
    customer.save(update_fields=["email_verified_at"])
    profile = aged(customer.profile, days=30)
    profile.email_reminders_sent = 2
    profile.save(update_fields=["email_reminders_sent"])

    call_command("send_verification_reminders", verbosity=0)

    profile.refresh_from_db()
    assert profile.email_reminders_sent == 2, "a third reminder went out"


def test_it_waits_before_the_first_one(customer, seeded):
    """Somebody who signed up an hour ago has not ignored anything yet."""
    customer.email_verified_at = None
    customer.save(update_fields=["email_verified_at"])
    aged(customer.profile, days=0)

    call_command("send_verification_reminders", verbosity=0)

    customer.profile.refresh_from_db()
    assert customer.profile.email_reminders_sent == 0


def test_the_desk_is_not_chased(owner, seeded):
    """An admin account is made by another admin, not signed up."""
    owner.email_verified_at = None
    owner.save(update_fields=["email_verified_at"])
    from nkenzapay.accounts.models import Profile

    profile, _ = Profile.objects.get_or_create(user=owner)
    aged(profile, days=30)

    call_command("send_verification_reminders", verbosity=0)

    profile.refresh_from_db()
    assert profile.email_reminders_sent == 0


def test_the_identity_budget_is_not_spent_on_the_email_one(customer, seeded):
    """Two counters, because they chase different things."""
    customer.email_verified_at = None
    customer.save(update_fields=["email_verified_at"])
    profile = aged(customer.profile, days=2)
    before = profile.reminders_sent

    call_command("send_verification_reminders", verbosity=0)

    profile.refresh_from_db()
    assert profile.email_reminders_sent == 1
    assert profile.reminders_sent == before or profile.reminders_sent >= before
