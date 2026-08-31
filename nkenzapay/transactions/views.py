from django.db.models import Prefetch
from django.http import FileResponse, Http404
from rest_framework import generics, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from nkenzapay.common.crypto import DecryptionError
from nkenzapay.common.exceptions import DomainError
from nkenzapay.security import services as security
from nkenzapay.security.idempotency import idempotent
from nkenzapay.security.models import EventKind

from . import services
from .models import Attachment, Message, Status, Transaction
from .serializers import (
    AttachmentSerializer,
    CreateTransactionSerializer,
    MessageSerializer,
    ReceiptSerializer,
    TransactionDetailSerializer,
    TransactionListSerializer,
)
from .uploads import commit_upload, may_access, request_upload_url, signed_url_for


class TransactionListCreate(generics.ListCreateAPIView):
    """List a customer's transfers, or open a new one.

    Creation is idempotent. A double tap on Create order, or a retry after a
    dropped connection, must not produce two transfers and two payments.
    """

    permission_classes = [IsAuthenticated]
    throttle_scope = "order"

    def get_serializer_class(self):
        return CreateTransactionSerializer if self.request.method == "POST" \
            else TransactionListSerializer

    def get_queryset(self):
        queryset = (
            Transaction.objects.for_user(self.request.user)
            .select_related("corridor__source", "corridor__target", "collect_method",
                            "send_currency", "receive_currency")
        )
        params = self.request.query_params
        if params.get("direction"):
            queryset = queryset.filter(direction=params["direction"])
        if params.get("status"):
            wanted = params["status"]
            if wanted == "open":
                queryset = queryset.open()
            else:
                queryset = queryset.filter(status__in=wanted.split(","))
        if params.get("from"):
            queryset = queryset.filter(created_at__date__gte=params["from"])
        if params.get("to"):
            queryset = queryset.filter(created_at__date__lte=params["to"])
        return queryset

    @idempotent
    def create(self, request, *args, **kwargs):
        form = CreateTransactionSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        data = form.validated_data

        txn = services.create_transaction(
            user=request.user,
            quote=data["quote_reference"],
            collect_method=data["collect_method"],
            recipient={
                "name": data.get("recipient_name", ""),
                "number": data.get("recipient_number", ""),
                "details": data.get("recipient_details", {}),
            },
            request=request,
        )
        payload = TransactionDetailSerializer(txn, context={"request": request}).data
        return Response(payload, status=status.HTTP_201_CREATED)


class TransactionDetail(generics.RetrieveAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = TransactionDetailSerializer
    lookup_field = "reference"

    def get_queryset(self):
        queryset = Transaction.objects.select_related(
            "user__profile", "corridor__source", "corridor__target", "collect_method",
            "send_currency", "receive_currency", "receipt",
        ).prefetch_related("history")
        if not self.request.user.is_desk:
            queryset = queryset.for_user(self.request.user)
        return queryset


class MessageListCreate(generics.ListCreateAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = MessageSerializer
    pagination_class = None
    throttle_scope = "message"

    def get_transaction(self):
        txn = generics.get_object_or_404(Transaction, reference=self.kwargs["reference"])
        if not may_access(txn, self.request.user):
            raise Http404
        return txn

    def get_queryset(self):
        txn = self.get_transaction()
        queryset = txn.messages.select_related("sender").prefetch_related("attachments")
        after = self.request.query_params.get("after")
        if after:
            queryset = queryset.filter(id__gt=after)
        return queryset

    @idempotent
    def create(self, request, *args, **kwargs):
        txn = self.get_transaction()
        body = (request.data.get("body") or "").strip()
        if not body:
            raise DomainError("empty_message", "Write something before you send it.")
        if len(body) > 4000:
            raise DomainError("message_too_long", "That message is too long to send.")

        message = services.post_message(
            reference=txn.reference,
            sender=request.user,
            body=body,
            is_from_desk=bool(request.user.is_desk),
            request=request,
        )
        return Response(MessageSerializer(message, context={"request": request}).data,
                        status=status.HTTP_201_CREATED)


class MarkThreadRead(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, reference):
        from django.utils import timezone

        txn = generics.get_object_or_404(Transaction, reference=reference)
        if not may_access(txn, request.user):
            raise Http404
        # Mark the other side's messages, never your own.
        mine_is_desk = bool(request.user.is_desk)
        Message.objects.filter(
            transaction=txn, read_at__isnull=True
        ).exclude(is_from_desk=mine_is_desk).update(read_at=timezone.now())
        return Response(status=status.HTTP_204_NO_CONTENT)


class AttachmentUploadUrl(APIView):
    permission_classes = [IsAuthenticated]
    throttle_scope = "upload"

    def post(self, request, reference):
        txn = generics.get_object_or_404(Transaction, reference=reference)
        return Response(request_upload_url(
            transaction=txn,
            user=request.user,
            content_type=request.data.get("content_type", ""),
            size_bytes=int(request.data.get("size_bytes") or 0),
            filename=request.data.get("filename", ""),
        ))


class AttachmentCommit(APIView):
    permission_classes = [IsAuthenticated]
    throttle_scope = "upload"

    def post(self, request, reference):
        txn = generics.get_object_or_404(Transaction, reference=reference)
        try:
            attachment = commit_upload(
                transaction=txn,
                user=request.user,
                key=request.data.get("key", ""),
                original_name=request.data.get("filename", ""),
                content_type=request.data.get("content_type", ""),
                size_bytes=int(request.data.get("size_bytes") or 0),
                is_payment_proof=bool(request.data.get("is_payment_proof")),
                request=request,
            )
        except DomainError as exc:
            security.record(
                EventKind.BAD_UPLOAD,
                request=request,
                summary=f"Upload rejected: {exc.code}",
                user=request.user,
                detail={
                    "reason": exc.code,
                    "declared_type": str(request.data.get("content_type", ""))[:60],
                    "declared_size": request.data.get("size_bytes"),
                    "reference": reference,
                },
            )
            raise
        return Response(AttachmentSerializer(attachment, context={"request": request}).data,
                        status=status.HTTP_201_CREATED)


class AttachmentUrl(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        attachment = generics.get_object_or_404(
            Attachment.objects.select_related("transaction"), pk=pk
        )
        return Response({"url": signed_url_for(attachment, request.user),
                         "expires_in": 60})


class TransactionAction(APIView):
    """The customer's four buttons: I have paid, I received the money,
    I have not received the money, cancel.

    Idempotent, because every one of these moves the transfer forward and a
    repeated tap must not move it twice.
    """

    permission_classes = [IsAuthenticated]
    throttle_scope = "order"

    @idempotent
    def post(self, request, reference, action):
        if action == "paid":
            txn = services.customer_paid(reference=reference, user=request.user,
                                         request=request)
        elif action == "received":
            txn, _receipt = services.confirm_received(reference=reference,
                                                      user=request.user, request=request)
        elif action == "not-received":
            services.open_dispute(
                reference=reference, user=request.user,
                reason_code=request.data.get("reason_code", "not_received"),
                detail=request.data.get("detail", ""), request=request,
            )
            txn = Transaction.objects.get(reference=reference)
        elif action == "cancel":
            txn = Transaction.objects.get(reference=reference)
            if txn.user_id != request.user.id:
                raise DomainError("not_yours", "This transfer belongs to another account.")
            txn = services.cancel(reference=reference, actor=request.user,
                                  reason=request.data.get("reason", ""), request=request)
        else:
            raise DomainError("unknown_action", "That action does not exist.")

        return Response(TransactionDetailSerializer(txn, context={"request": request}).data)


class DisputeCreate(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, reference):
        from nkenzapay.disputes.models import Dispute

        reason = request.data.get("reason_code", "")
        if reason not in dict(Dispute.REASONS):
            raise DomainError("bad_reason", "Pick one of the listed problems.")
        dispute = services.open_dispute(
            reference=reference, user=request.user, reason_code=reason,
            detail=request.data.get("detail", ""), request=request,
        )
        return Response({"id": dispute.pk, "state": dispute.state,
                         "reason": dispute.reason_display},
                        status=status.HTTP_201_CREATED)


class ReceiptView(generics.RetrieveAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = ReceiptSerializer

    def get_object(self):
        txn = generics.get_object_or_404(Transaction, reference=self.kwargs["reference"])
        if not may_access(txn, self.request.user):
            raise Http404
        receipt = getattr(txn, "receipt", None)
        if receipt is None:
            raise DomainError("no_receipt", "A receipt exists once the transfer completes.")
        return receipt


class ReceiptPdfView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, reference):
        from .receipts import render_pdf

        txn = generics.get_object_or_404(Transaction, reference=reference)
        if not may_access(txn, request.user):
            raise Http404
        receipt = getattr(txn, "receipt", None)
        if receipt is None:
            raise DomainError("no_receipt", "A receipt exists once the transfer completes.")

        buffer = render_pdf(receipt)
        return FileResponse(buffer, as_attachment=True,
                            filename=f"NkenzaPay-{txn.reference}.pdf",
                            content_type="application/pdf")


class LocalUploadView(APIView):
    """Development storage endpoint.

    Signed both ways: a PUT accepts one upload for one key, a GET serves it for
    sixty seconds. In production the same calls are answered by object storage
    and this view is never reached.
    """

    permission_classes = [AllowAny]

    def put(self, request, signed):
        from nkenzapay.common.storage import LocalStorage, storage

        backend = storage()
        if not isinstance(backend, LocalStorage):
            raise Http404
        key = backend.verify_signed_key(signed, ttl=600)
        if key is None:
            raise DomainError("bad_upload_url", "That upload link has expired.")
        backend.save_bytes(key, request.body, request.content_type or "")
        return Response({"key": key}, status=status.HTTP_201_CREATED)

    def get(self, request, signed):
        from nkenzapay.common.storage import LocalStorage, storage

        backend = storage()
        if not isinstance(backend, LocalStorage):
            raise Http404
        key = backend.verify_signed_key(signed)
        if key is None:
            raise DomainError("link_expired", "That link has expired. Reload the page.")
        try:
            data = backend.read_bytes(key)
        except (FileNotFoundError, OSError) as exc:
            raise Http404 from exc
        except DecryptionError as exc:
            # Sealed with a key this deployment no longer holds. Say so rather
            # than serving the ciphertext, which would look like a corrupt file
            # and send somebody hunting for a bug that is not there.
            raise DomainError(
                "file_unreadable",
                "That file cannot be opened on this server. Contact support.",
                {"key_missing": True},
            ) from exc

        import io
        import mimetypes

        content_type = mimetypes.guess_type(key)[0] or "application/octet-stream"
        response = FileResponse(io.BytesIO(data), content_type=content_type)

        # An identity photograph must not sit in a shared cache, a proxy, or
        # the browser's disk cache after the one-minute link has expired.
        response["Cache-Control"] = "private, no-store, max-age=0"
        response["X-Content-Type-Options"] = "nosniff"
        # Images are rendered in the thread; everything else is handed over as
        # a download rather than opened in place, so nothing a customer uploads
        # is ever executed by a viewer on this origin.
        if not content_type.startswith("image/"):
            response["Content-Disposition"] = "attachment"
        return response
