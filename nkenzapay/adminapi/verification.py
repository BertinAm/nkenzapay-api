"""Approving accounts.

Somebody on the desk looks at a photograph of a government document and decides
whether the name on it is the name on the account. Nothing here is automated and
nothing here scores anybody: it records which person decided, when, and what
they told the customer.

The document is served through a short-lived signed URL like every other private
file, so a screenshot of this screen does not carry a working link to a
customer's passport.
"""
from django.utils import timezone
from rest_framework import generics
from rest_framework.response import Response
from rest_framework.views import APIView

from nkenzapay.accounts.models import Profile
from nkenzapay.audit import services as audit
from nkenzapay.common.exceptions import DomainError
from nkenzapay.common.storage import storage
from nkenzapay.notifications import services as notifications

from .permissions import IsDesk


class VerificationQueue(generics.ListAPIView):
    """Accounts waiting on a decision, oldest first.

    Oldest first on purpose. A queue worked newest-first leaves whoever
    submitted on Monday still waiting on Friday.
    """

    permission_classes = [IsDesk]

    def get(self, request):
        state = request.query_params.get("state", Profile.PENDING)
        rows = (
            Profile.objects.filter(verification_state=state)
            .select_related("user", "country", "verified_by")
            .order_by("id_submitted_at")
        )

        counts = {
            key: Profile.objects.filter(verification_state=key).count()
            for key, _ in Profile.VERIFICATION_STATES
        }

        page = self.paginate_queryset(rows)
        if page is not None:
            response = self.get_paginated_response([_row(p) for p in page])
            response.data["counts"] = counts
            return response
        return Response({"results": [_row(p) for p in rows], "counts": counts})


class VerificationDetail(APIView):
    """One account, with a link to the document that expires shortly."""

    permission_classes = [IsDesk]

    def get(self, request, pk):
        profile = generics.get_object_or_404(
            Profile.objects.select_related("user", "country", "verified_by"), pk=pk
        )
        audit.record(
            actor=request.user, action="verification.opened",
            summary=f"{request.user.email} opened {profile.user.email} for verification",
            target=profile, request=request,
        )
        return Response(_row(profile, with_document=True))


class VerificationDecide(APIView):
    """Approve or reject.

    Approving is what lets money move for that account, so it asks for the same
    trust the money does. Rejecting needs a reason, because the customer is
    about to be asked for another document and has to know what was wrong with
    the first one.
    """

    permission_classes = [IsDesk]

    def post(self, request, pk, action):
        profile = generics.get_object_or_404(
            Profile.objects.select_related("user"), pk=pk
        )

        admin_profile = getattr(request.user, "admin_profile", None)
        if admin_profile is None or not admin_profile.can_move_money:
            raise DomainError("not_permitted", "This account cannot approve customers.")

        if action == "approve":
            return self._approve(request, profile)
        if action == "reject":
            return self._reject(request, profile)
        raise DomainError("unknown_action", "Approve or reject.")

    def _approve(self, request, profile):
        if not profile.id_document_key:
            raise DomainError(
                "nothing_to_approve", "This account has not sent a document yet."
            )

        before = profile.verification_state
        profile.verification_state = Profile.APPROVED
        profile.verified_at = timezone.now()
        profile.verified_by = request.user
        profile.verification_note = ""
        profile.reminders_sent = 0
        profile.save(update_fields=[
            "verification_state", "verified_at", "verified_by",
            "verification_note", "reminders_sent",
        ])

        notifications.notify(
            profile.user, "account.id_approved",
            email_body=(
                "Your account is approved. You can send and receive money now.\n\n"
                "Every transfer opens its own chat with the desk, so there is "
                "always someone to ask."
            ),
        )
        audit.record(
            actor=request.user, action="verification.approved",
            summary=f"{request.user.email} approved {profile.user.email}",
            target=profile, before={"state": before},
            after={"state": profile.verification_state}, request=request,
        )
        return Response(_row(profile))

    def _reject(self, request, profile):
        note = (request.data.get("note") or "").strip()
        if not note:
            raise DomainError(
                "reason_required",
                "Say what was wrong with it. The customer is about to be asked "
                "for another one and needs to know what to change.",
            )

        before = profile.verification_state
        profile.verification_state = Profile.REJECTED
        profile.verification_note = note
        profile.verified_at = None
        profile.verified_by = request.user
        profile.reminders_sent = 0
        profile.last_reminder_at = None
        profile.save(update_fields=[
            "verification_state", "verification_note", "verified_at",
            "verified_by", "reminders_sent", "last_reminder_at",
        ])

        notifications.notify(
            profile.user, "account.id_rejected",
            context={"detail": note},
            email_body=(
                "We could not approve the document you sent.\n\n"
                f"{note}\n\n"
                "Send another one and the desk will look again."
            ),
        )
        audit.record(
            actor=request.user, action="verification.rejected",
            summary=(f"{request.user.email} rejected {profile.user.email}: {note[:120]}"),
            target=profile, before={"state": before},
            after={"state": profile.verification_state}, request=request,
        )
        return Response(_row(profile))


def _row(profile, with_document=False):
    user = profile.user
    row = {
        "id": profile.pk,
        "user_id": user.pk,
        "name": user.display_name,
        "initials": user.initials,
        "email": user.email,
        "whatsapp": profile.whatsapp_display,
        "legal_name": profile.legal_name,
        "country": profile.country_id,
        "state": profile.verification_state,
        "document_type": profile.id_document_type,
        "submitted_at": profile.id_submitted_at,
        "verified_at": profile.verified_at,
        "verified_by": profile.verified_by.display_name if profile.verified_by_id else None,
        "note": profile.verification_note,
        "reminders_sent": profile.reminders_sent,
        "missing": profile.missing_steps,
        "joined": user.date_joined,
    }
    if with_document:
        # Minutes, not hours. Long enough to read, short enough that a copied
        # link is worth nothing by the time it is pasted anywhere else.
        row["photo_url"] = _signed(profile.photo_key)
        row["document_url"] = _signed(profile.id_document_key)
    return row


def _signed(key):
    if not key:
        return None
    return storage().presign_get(key, ttl=300)
