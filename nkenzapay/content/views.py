import secrets

import bleach
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from nkenzapay.common.exceptions import DomainError

from .models import LegalDocument, NewsletterSubscriber, NewsPost
from .serializers import (
    LegalDocumentSerializer,
    NewsDetailSerializer,
    NewsListSerializer,
)


class NewsList(generics.ListAPIView):
    permission_classes = [AllowAny]
    serializer_class = NewsListSerializer

    def get_queryset(self):
        return NewsPost.objects.filter(
            is_published=True, publish_at__lte=timezone.now()
        )


class NewsDetail(generics.RetrieveAPIView):
    permission_classes = [AllowAny]
    serializer_class = NewsDetailSerializer
    lookup_field = "slug"

    def get_queryset(self):
        return NewsPost.objects.filter(is_published=True, publish_at__lte=timezone.now())

    def retrieve(self, request, *args, **kwargs):
        post = self.get_object()
        NewsPost.objects.filter(pk=post.pk).update(view_count=post.view_count + 1)
        return Response(self.get_serializer(post).data)


class NewsletterSubscribe(APIView):
    """Double opt-in. Subscribing does not confirm anything on its own."""

    permission_classes = [AllowAny]
    throttle_scope = "auth"

    def post(self, request):
        email = (request.data.get("email") or "").strip().lower()
        if "@" not in email:
            raise DomainError("bad_email", "Enter an email address we can send to.")

        subscriber, created = NewsletterSubscriber.objects.get_or_create(
            email=email,
            defaults={
                "confirm_token": secrets.token_urlsafe(24),
                "source": request.data.get("source", "")[:40],
                "user": request.user if request.user.is_authenticated else None,
            },
        )
        if not created and subscriber.unsubscribed_at:
            subscriber.unsubscribed_at = None
            subscriber.confirm_token = secrets.token_urlsafe(24)
            subscriber.confirmed_at = None
            subscriber.save(update_fields=["unsubscribed_at", "confirm_token", "confirmed_at"])

        # The confirmation email carries subscriber.confirm_token. It is never
        # returned here — that would defeat the opt-in.
        return Response({"pending": True})


class NewsletterConfirm(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        token = request.data.get("token", "")
        subscriber = NewsletterSubscriber.objects.filter(confirm_token=token).first()
        if subscriber is None:
            raise DomainError("bad_token", "That confirmation link is no longer valid.")
        subscriber.confirmed_at = timezone.now()
        subscriber.save(update_fields=["confirmed_at"])
        return Response({"confirmed": True})


class NewsletterUnsubscribe(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        token = request.data.get("token", "")
        email = (request.data.get("email") or "").strip().lower()
        subscriber = NewsletterSubscriber.objects.filter(confirm_token=token).first()
        if subscriber is None and email:
            subscriber = NewsletterSubscriber.objects.filter(email=email).first()
        if subscriber is not None:
            subscriber.unsubscribed_at = timezone.now()
            subscriber.save(update_fields=["unsubscribed_at"])
        return Response({"unsubscribed": True})


class LegalDocumentView(generics.RetrieveAPIView):
    permission_classes = [AllowAny]
    serializer_class = LegalDocumentSerializer
    queryset = LegalDocument.objects.all()
    lookup_field = "slug"


class SupportReport(APIView):
    """The general Report a problem form on the help page.

    A problem about a specific transfer belongs in that transfer's chat, where
    the desk can see the amount and the proof. When a reference comes through
    here, the report is routed there instead of into a separate queue.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        from nkenzapay.disputes.models import Dispute
        from nkenzapay.notifications import services as notifications
        from nkenzapay.transactions import services as txn_services
        from nkenzapay.transactions.models import Transaction

        reason = request.data.get("reason_code", "other")
        if reason not in dict(Dispute.REASONS):
            raise DomainError("bad_reason", "Pick one of the listed problems.")
        detail = bleach.clean(request.data.get("detail", ""), tags=[], strip=True)[:2000]
        reference = request.data.get("reference", "")

        if reference:
            txn = Transaction.objects.filter(
                reference=reference, user=request.user
            ).first()
            if txn is None:
                raise DomainError("unknown_transfer", "We cannot find that transfer.")
            dispute = txn_services.open_dispute(
                reference=txn.reference, user=request.user,
                reason_code=reason, detail=detail, request=request,
            )
            return Response({"routed_to_chat": True, "reference": txn.reference,
                             "dispute": dispute.pk}, status=status.HTTP_201_CREATED)

        notifications.notify_desk(
            "admin.dispute_opened",
            context={"customer": request.user.display_name,
                     "reference": "no transfer given"},
        )
        return Response({"routed_to_chat": False}, status=status.HTTP_201_CREATED)
