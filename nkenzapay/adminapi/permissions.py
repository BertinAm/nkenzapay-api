"""Desk permissions.

Gated per action rather than per view, because the same screen holds actions a
Support operator may take and ones they may not. A role that cannot verify a
payment can still read the transfer and answer the chat.

Two-factor is enforced here as well as in the transaction services. Belt and
braces on the one boundary where a mistake moves real money.
"""
from rest_framework.permissions import BasePermission

from nkenzapay.accounts.models import AdminRole


def admin_profile(request):
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return None
    return getattr(user, "admin_profile", None)


class IsDesk(BasePermission):
    """Any admin role. Enough to read the queue and the analytics."""

    message = "This area is for the NkenzaPay desk."

    def has_permission(self, request, view):
        return admin_profile(request) is not None


class CanMoveMoney(BasePermission):
    """Verify, reject, payout, resolve a dispute."""

    message = "This account cannot act on payments."

    def has_permission(self, request, view):
        profile = admin_profile(request)
        if profile is None or not profile.can_move_money:
            return False
        return profile.totp_confirmed_at is not None


class CanChat(BasePermission):
    message = "This account cannot reply to customers."

    def has_permission(self, request, view):
        profile = admin_profile(request)
        return profile is not None and profile.can_chat


class CanWriteSettings(BasePermission):
    """Fees, limits, payment details, countries, admin accounts. Owner only."""

    message = "Only an owner account can change platform settings."

    def has_permission(self, request, view):
        profile = admin_profile(request)
        if profile is None:
            return False
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return True
        return profile.can_write_settings


class ReadOnlyForReadOnlyRole(BasePermission):
    """A read-only account may look at anything the desk sees and change none
    of it."""

    def has_permission(self, request, view):
        profile = admin_profile(request)
        if profile is None:
            return False
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return True
        return profile.role != AdminRole.READ_ONLY
