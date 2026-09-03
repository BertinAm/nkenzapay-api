from datetime import timedelta
from decimal import Decimal

from django.db.models import Avg, Count, OuterRef, Q, Subquery, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.views import APIView

from nkenzapay.accounts.models import AdminUser, LoginActivity, User
from nkenzapay.accounts.serializers import LoginActivitySerializer
from nkenzapay.analytics.models import ExportJob, PageView
from nkenzapay.audit import services as audit
from nkenzapay.audit.models import AuditEntry
from nkenzapay.common.exceptions import DomainError
from nkenzapay.content.models import NewsPost
from nkenzapay.content.serializers import AdminNewsSerializer
from nkenzapay.disputes.models import Dispute
from nkenzapay.geo.models import Corridor, Country
from nkenzapay.geo.serializers import CorridorSerializer, CountrySerializer
from nkenzapay.notifications.models import DeliveryRule, Notification
from nkenzapay.security.models import SecurityEvent, Severity
from nkenzapay.payments.models import PaymentInstruction, PaymentMethod
from nkenzapay.payments.serializers import AdminPaymentMethodSerializer
from nkenzapay.pricing.models import FeeRule, PlatformSetting, TransferLimit
from nkenzapay.rates.models import RateProvider, RateSnapshot
from nkenzapay.transactions import services as txn_services
from nkenzapay.transactions.models import (
    Message,
    Status,
    StatusHistory,
    Transaction,
)
from nkenzapay.transactions.serializers import (
    MessageSerializer,
    TransactionDetailSerializer,
    money,
)

from .permissions import CanChat, CanMoveMoney, CanWriteSettings, IsDesk
from .serializers import (
    AdminTransactionListSerializer,
    AdminUserSerializer,
    AuditEntrySerializer,
    CustomerDetailSerializer,
    CustomerListSerializer,
    DisputeSerializer,
    ExportJobSerializer,
    FeeRuleSerializer,
    RateProviderSerializer,
    ThreadSummarySerializer,
    TransferLimitSerializer,
)


def parse_range(request, default_days=30):
    """Today, 7D, 30D, 3M, 6M, 1Y or a custom pair. One helper, every screen."""
    table = {"today": 1, "7d": 7, "30d": 30, "3m": 90, "6m": 182, "1y": 365}
    wanted = (request.query_params.get("range") or f"{default_days}d").lower()
    if wanted == "custom":
        start = request.query_params.get("from")
        end = request.query_params.get("to")
        if start and end:
            return (timezone.datetime.fromisoformat(start).replace(tzinfo=timezone.utc),
                    timezone.datetime.fromisoformat(end).replace(tzinfo=timezone.utc))
    days = table.get(wanted, default_days)
    now = timezone.now()
    return now - timedelta(days=days), now


# --- Overview and queue --------------------------------------------------


class Overview(APIView):
    permission_classes = [IsDesk]

    def get(self, request):
        since, until = parse_range(request)
        rows = Transaction.objects.filter(created_at__range=(since, until))
        today = Transaction.objects.filter(created_at__date=timezone.now().date())

        processed = today.aggregate(
            xaf=Sum("send_amount", filter=Q(send_currency="XAF")),
            inr=Sum("send_amount", filter=Q(send_currency="INR")),
            fees=Sum("fee_amount"),
        )
        # Yesterday, for the comparison under each headline figure. Same
        # calendar-day boundary as `today`, so the two are like for like.
        yesterday = Transaction.objects.filter(
            created_at__date=timezone.now().date() - timedelta(days=1)
        )
        before = yesterday.aggregate(
            inr=Sum("send_amount", filter=Q(send_currency="INR")),
            fees=Sum("fee_amount"),
        )

        pending = Transaction.objects.needs_desk().select_related(
            "user__profile", "collect_method", "send_currency", "receive_currency",
            "corridor__source", "corridor__target",
        ).annotate(status_since=_status_since())[:10]

        return Response({
            "kpis": {
                "registered_users": User.objects.count(),
                "new_users": User.objects.filter(date_joined__range=(since, until)).count(),
                "transfers_today": today.count(),
                # Formatted here, like every other figure: INR groups by lakh
                # and XAF has no subunit, and the desk should read the same
                # string the customer was shown.
                "processed_today_inr": money(processed["inr"] or 0, "INR"),
                "processed_today_xaf": money(processed["xaf"] or 0, "XAF"),
                "fees_today": money(processed["fees"] or 0, "INR"),
                "waiting_on_desk": Transaction.objects.needs_desk().count(),
                "open_disputes": Dispute.objects.filter(state=Dispute.OPEN).count(),
            },
            "deltas": {
                "transfers": _change(today.count(), yesterday.count()),
                "processed_inr": _change(processed["inr"], before["inr"]),
                "fees": _change(processed["fees"], before["fees"]),
            },
            "volume_series": self.volume_series(since, until),
            "by_method": self.by_method(rows),
            "pending": AdminTransactionListSerializer(pending, many=True).data,
        })

    def volume_series(self, since, until):
        rows = (
            Transaction.objects.filter(created_at__range=(since, until))
            .annotate(day=TruncDate("created_at"))
            .values("day", "direction")
            .annotate(count=Count("id"))
            .order_by("day")
        )
        series = {}
        for row in rows:
            key = row["day"].isoformat()
            series.setdefault(key, {"day": key, "receive": 0, "send": 0})
            series[key][row["direction"]] = row["count"]
        return list(series.values())

    def by_method(self, queryset):
        rows = (
            queryset.values("collect_method__label")
            .annotate(count=Count("id"))
            .order_by("-count")
        )
        total = sum(r["count"] for r in rows) or 1
        return [
            {"label": r["collect_method__label"], "count": r["count"],
             "percent": round(r["count"] * 100 / total, 1)}
            for r in rows
        ]


def _change(now_value, before_value):
    """Today against yesterday, as a percentage.

    Null when yesterday was zero. A rise from nothing is not a percentage, and
    rendering it as +100% tells the desk a story about a number that has no
    baseline.
    """
    now_value = Decimal(now_value or 0)
    before_value = Decimal(before_value or 0)
    if before_value == 0:
        return None
    percent = (now_value - before_value) / before_value * 100
    return {
        "percent": str(percent.quantize(Decimal("0.1"))),
        "direction": "up" if percent > 0 else "down" if percent < 0 else "flat",
    }


def _status_since():
    """When the transaction last changed state, as a scalar subquery.

    An aggregate annotation would add a GROUP BY, and a grouped queryset loses
    its default ordering — which paginates a desk queue in an order nobody
    chose. A correlated subquery reads the same value and leaves the query
    shape alone.
    """
    return Subquery(
        StatusHistory.objects.filter(transaction=OuterRef("pk"))
        .order_by("-at")
        .values("at")[:1]
    )


class Badges(APIView):
    """The counts beside each item in the desk sidebar.

    One request, because five screens' worth of badges polled separately is
    five times the load for a number nobody reads twice. Each count is the
    number of things asking for a person's attention — not a total.
    """

    permission_classes = [IsDesk]

    def get(self, request):
        day_ago = timezone.now() - timedelta(days=1)
        return Response({
            "transactions": Transaction.objects.needs_desk().count(),
            "messages": Transaction.objects.filter(
                messages__read_at__isnull=True, messages__is_from_desk=False
            ).distinct().count(),
            "notifications": Notification.objects.filter(
                user=request.user, audience=Notification.ADMIN, read_at__isnull=True
            ).count(),
            "disputes": Dispute.objects.filter(state=Dispute.OPEN).count(),
            # Only what is worth waking up for. Every scanner on the internet
            # produces low-severity noise, and a badge that is never zero is a
            # badge nobody looks at.
            "security": SecurityEvent.objects.filter(
                at__gte=day_ago,
                severity__in=[Severity.HIGH, Severity.CRITICAL],
            ).count(),
        })


class AdminTransactionList(generics.ListAPIView):
    permission_classes = [IsDesk]
    serializer_class = AdminTransactionListSerializer

    def get_queryset(self):
        queryset = Transaction.objects.select_related(
            "user__profile", "collect_method", "corridor__source", "corridor__target",
            "send_currency", "receive_currency",
        ).annotate(status_since=_status_since())
        params = self.request.query_params
        wanted = params.get("status", "all")
        if wanted == "needs_desk":
            queryset = queryset.needs_desk()
        elif wanted == "awaiting_payment":
            queryset = queryset.filter(status=Status.AWAITING_PAYMENT)
        elif wanted == "payout_due":
            queryset = queryset.filter(
                status__in=[Status.PAYMENT_CONFIRMED, Status.PAYOUT_PROCESSING]
            )
        elif wanted not in ("", "all"):
            queryset = queryset.filter(status__in=wanted.split(","))

        search = params.get("q")
        if search:
            queryset = queryset.filter(
                Q(reference__icontains=search)
                | Q(user__email__icontains=search)
                | Q(user__profile__first_name__icontains=search)
                | Q(user__profile__last_name__icontains=search)
                | Q(user__profile__whatsapp_number__icontains=search)
                | Q(recipient_number__icontains=search)
            )
        return queryset

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        response.data["counts"] = {
            "all": Transaction.objects.count(),
            "needs_desk": Transaction.objects.needs_desk().count(),
            "awaiting_payment": Transaction.objects.filter(
                status=Status.AWAITING_PAYMENT).count(),
            "payout_due": Transaction.objects.filter(
                status__in=[Status.PAYMENT_CONFIRMED, Status.PAYOUT_PROCESSING]).count(),
            "completed": Transaction.objects.filter(status=Status.COMPLETED).count(),
            "disputed": Transaction.objects.filter(status=Status.DISPUTED).count(),
            "cancelled": Transaction.objects.filter(status=Status.CANCELLED).count(),
        }
        return response


class AdminTransactionDetail(APIView):
    """Everything the verification screen shows in one payload."""

    permission_classes = [IsDesk]

    def get(self, request, reference):
        from nkenzapay.transactions.serializers import AttachmentSerializer

        txn = generics.get_object_or_404(
            Transaction.objects.select_related(
                "user__profile", "collect_method__instruction", "corridor__source",
                "corridor__target", "send_currency", "receive_currency",
            ).prefetch_related("history", "attachments"),
            reference=reference,
        )
        # One row per sitting, not one per render. The screen reloads itself
        # after every decision, and a desk that refreshes while waiting for a
        # payment would otherwise bury the entry that says what it decided.
        opened_recently = AuditEntry.objects.filter(
            actor=request.user,
            action="transaction.opened",
            target_id=str(txn.pk),
            at__gte=timezone.now() - timedelta(minutes=10),
        ).exists()
        if not opened_recently:
            audit.record(actor=request.user, action="transaction.opened",
                         summary=f"{request.user.email} opened {txn.reference} for verification",
                         target=txn, request=request)

        profile = getattr(txn.user, "profile", None)
        instruction = txn.instruction
        customer_counts = Transaction.objects.filter(user=txn.user).aggregate(
            transfers=Count("id"),
            completed=Count("id", filter=Q(status=Status.COMPLETED)),
        )

        return Response({
            "transaction": TransactionDetailSerializer(txn, context={"request": request}).data,
            "customer": {
                "id": txn.user_id,
                "name": txn.user.display_name,
                "initials": txn.user.initials,
                "email": txn.user.email,
                "whatsapp": profile.whatsapp_display if profile else "",
                "country": profile.country_id if profile else None,
                "member_since": txn.user.date_joined,
                "photo_url": _photo_url(profile),
                "transfers": customer_counts["transfers"],
                "completed_transfers": customer_counts["completed"],
                "disputes": Dispute.objects.filter(transaction__user=txn.user).count(),
            },
            "attachments": AttachmentSerializer(
                txn.attachments.all(), many=True, context={"request": request}
            ).data,
            "waiting_minutes": _waiting_minutes(txn),
            "expected": {
                "amount": money(txn.send_amount, txn.send_currency_id),
                "currency": txn.send_currency_id,
                "to": (instruction.ordered_fields().get("number")
                       or instruction.ordered_fields().get("upi_id") or "") if instruction else "",
                "reference": txn.payment_reference,
            },
            "payout": {
                "rate_used": str(txn.rate_used),
                "fee_percent": str(txn.fee_percent),
                "fee_amount": money(txn.fee_amount, txn.receive_currency_id),
                "pay_customer": money(txn.receive_amount, txn.receive_currency_id),
                "currency": txn.receive_currency_id,
            },
            "risk": _risk_checks(txn),
            "audit": AuditEntrySerializer(
                AuditEntry.objects.filter(target_id=str(txn.pk))[:20], many=True
            ).data,
        })


def _waiting_minutes(txn):
    """How long the desk has been the thing holding this transfer up.

    Null when it is not: a transfer waiting on the customer to pay is not a
    queue the desk can clear, and counting it as one makes the board lie.
    """
    if txn.status not in (Status.PROOF_SUBMITTED, Status.PAYMENT_VERIFICATION):
        return None
    last = txn.history.all().last()
    since = last.at if last else txn.created_at
    return int((timezone.now() - since).total_seconds() // 60)


def _photo_url(profile):
    if not profile or not profile.photo_key:
        return None
    from nkenzapay.common.storage import storage

    return storage().presign_get(profile.photo_key, ttl=300)


def _risk_checks(txn):
    """The four rows on the left of the verification screen.

    Cheap, explainable checks. Nothing here blocks a transfer on its own; they
    exist so the person deciding has the context in front of them.
    """
    profile = getattr(txn.user, "profile", None)
    checks = []

    from nkenzapay.pricing.engine import resolve_limit

    limit = resolve_limit(txn.corridor, txn.direction)
    if limit and limit.daily_maximum:
        spent = Transaction.objects.filter(
            user=txn.user, send_currency=txn.send_currency,
            created_at__gte=timezone.now() - timedelta(days=1),
        ).exclude(status__in=[Status.CANCELLED, Status.REJECTED]).aggregate(
            total=Sum("send_amount"))["total"] or 0
        inside = spent <= limit.daily_maximum
        checks.append({"tone": "good" if inside else "warn",
                       "label": "Amount inside daily limit" if inside
                       else "Over the daily limit for this customer"})

    if profile and profile.whatsapp_number:
        shared = User.objects.filter(
            profile__whatsapp_number=profile.whatsapp_number
        ).exclude(pk=txn.user_id).count()
        checks.append({
            "tone": "good" if shared == 0 else "warn",
            "label": "One account on this number" if shared == 0
            else f"{shared + 1} accounts share this number",
        })

    recent = LoginActivity.objects.filter(user=txn.user, succeeded=True).first()
    if recent:
        checks.append({"tone": "warn" if recent.is_new_device else "good",
                       "label": "Login from a new device" if recent.is_new_device
                       else "Login from usual device"})

    if txn.needs_manual_review:
        checks.append({"tone": "warn", "label": "Held for manual review, high value"})

    return checks


class AdminTransactionAction(APIView):
    permission_classes = [CanMoveMoney]

    def post(self, request, reference, action):
        if action == "verify":
            txn = txn_services.verify_payment(
                reference=reference, admin_user=request.user,
                note=request.data.get("note", ""), request=request,
            )
        elif action == "reject":
            txn = txn_services.reject_payment(
                reference=reference, admin_user=request.user,
                reason=request.data.get("reason", ""), request=request,
            )
        elif action == "payout-sent":
            txn = txn_services.mark_payout_sent(
                reference=reference, admin_user=request.user, request=request,
            )
        elif action == "cancel":
            txn = txn_services.cancel(
                reference=reference, actor=request.user,
                reason=request.data.get("reason", ""), request=request,
            )
        else:
            raise DomainError("unknown_action", "That action does not exist.")
        return Response(TransactionDetailSerializer(txn, context={"request": request}).data)


# --- Messages inbox -------------------------------------------------------


class AdminInbox(APIView):
    permission_classes = [IsDesk]

    def get(self, request):
        threads = (
            Transaction.objects.exclude(messages__isnull=True)
            .select_related("user__profile", "send_currency", "receive_currency")
            .prefetch_related("messages")
            .order_by("-created_at")
        )
        wanted = request.query_params.get("filter")
        if wanted == "unread":
            threads = threads.filter(messages__read_at__isnull=True,
                                     messages__is_from_desk=False).distinct()
        elif wanted == "needs_desk":
            threads = threads.needs_desk()
        return Response({
            "threads": ThreadSummarySerializer(threads[:50], many=True).data,
            # Sent with the inbox rather than read from the settings endpoint,
            # which only accounts that can write settings may open. Someone
            # answering the chat all day is not necessarily one of them.
            "quick_replies": PlatformSetting.get("desk").get("quick_replies", []),
        })


class AdminReply(APIView):
    def get_permissions(self):
        """Reading a conversation is part of looking at a transfer. Writing
        into it, in the platform's own voice, is a separate thing to be
        trusted with."""
        return [CanChat()] if self.request.method == "POST" else [IsDesk()]

    def get(self, request, reference):
        txn = generics.get_object_or_404(Transaction, reference=reference)
        messages = txn.messages.select_related("sender").all()

        # The desk is looking at them now, so the unread badge should stop
        # saying otherwise. Only inbound messages: the desk's own were never
        # unread to it.
        txn.messages.filter(read_at__isnull=True, is_from_desk=False).update(
            read_at=timezone.now()
        )

        return Response(
            MessageSerializer(messages, many=True, context={"request": request}).data
        )

    def post(self, request, reference):
        body = (request.data.get("body") or "").strip()
        if not body:
            raise DomainError("empty_message", "Write something before you send it.")
        message = txn_services.post_message(
            reference=reference, sender=request.user, body=body,
            is_from_desk=True, request=request,
        )
        return Response(MessageSerializer(message, context={"request": request}).data,
                        status=status.HTTP_201_CREATED)


# --- Users ---------------------------------------------------------------


class AdminUserList(generics.ListAPIView):
    permission_classes = [IsDesk]
    serializer_class = CustomerListSerializer

    def get_queryset(self):
        queryset = User.objects.select_related("profile").annotate(
            transfer_count=Count("transactions")
        )
        search = self.request.query_params.get("q")
        if search:
            queryset = queryset.filter(
                Q(email__icontains=search)
                | Q(profile__first_name__icontains=search)
                | Q(profile__last_name__icontains=search)
                | Q(profile__whatsapp_number__icontains=search)
            )
        state = self.request.query_params.get("status")
        if state == "suspended":
            queryset = queryset.filter(is_suspended=True)
        elif state == "active":
            queryset = queryset.filter(is_suspended=False)
        return queryset

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        response.data["stats"] = {
            "registered": User.objects.count(),
            "active_this_month": User.objects.filter(
                last_seen_at__gte=timezone.now() - timedelta(days=30)).count(),
            "suspended": User.objects.filter(is_suspended=True).count(),
        }
        return response


class AdminUserDetail(generics.RetrieveAPIView):
    permission_classes = [IsDesk]
    serializer_class = CustomerDetailSerializer
    queryset = User.objects.select_related("profile__country")


class AdminUserSuspend(APIView):
    permission_classes = [CanWriteSettings]

    def post(self, request, pk, action):
        user = generics.get_object_or_404(User, pk=pk)
        reason = request.data.get("reason", "").strip()

        if action == "suspend":
            if not reason:
                raise DomainError("reason_required", "A suspension needs a reason.")
            user.is_suspended = True
            user.suspended_reason = reason
            user.suspended_at = timezone.now()
            summary = f"{request.user.email} suspended {user.email}: {reason[:120]}"
        else:
            user.is_suspended = False
            user.suspended_reason = ""
            user.suspended_at = None
            summary = f"{request.user.email} reactivated {user.email}"

        user.save(update_fields=["is_suspended", "suspended_reason", "suspended_at"])
        audit.record(actor=request.user, action=f"user.{action}", summary=summary,
                     target=user, after={"reason": reason}, request=request)
        return Response(CustomerDetailSerializer(user).data)


class AdminUserLoginActivity(generics.ListAPIView):
    permission_classes = [IsDesk]
    serializer_class = LoginActivitySerializer

    def get_queryset(self):
        return LoginActivity.objects.filter(user_id=self.kwargs["pk"])[:100]


# --- Settings -------------------------------------------------------------


class RateSettings(APIView):
    permission_classes = [CanWriteSettings]

    def get(self, request):
        provider = RateProvider.objects.filter(is_active=True).first()
        latest = {
            f"{s.base_id}/{s.quote_id}": str(s.effective_rate)
            for s in RateSnapshot.objects.order_by("base_id", "quote_id", "-fetched_at")
            .distinct("base_id", "quote_id")
        } if _supports_distinct_on() else _latest_rates_fallback()

        return Response({
            "provider": RateProviderSerializer(provider).data if provider else None,
            "providers": RateProviderSerializer(
                RateProvider.objects.all(), many=True).data,
            "latest": latest,
        })

    def put(self, request):
        provider = generics.get_object_or_404(
            RateProvider, pk=request.data.get("id") or _active_provider_id()
        )
        before = RateProviderSerializer(provider).data
        form = RateProviderSerializer(provider, data=request.data, partial=True)
        form.is_valid(raise_exception=True)
        provider = form.save()

        if request.data.get("is_active"):
            RateProvider.objects.exclude(pk=provider.pk).update(is_active=False)

        audit.record(actor=request.user, action="settings.rates_changed",
                     summary=(f"{request.user.email} changed the rate settings "
                              f"({provider.label}, markup {provider.markup_bps} bps)"),
                     target=provider, before=before, after=form.data, request=request)
        return Response(form.data)


def _active_provider_id():
    provider = RateProvider.objects.filter(is_active=True).first()
    return provider.pk if provider else 0


def _supports_distinct_on():
    from django.db import connection

    return connection.vendor == "postgresql"


def _latest_rates_fallback():
    seen = {}
    for snapshot in RateSnapshot.objects.order_by("-fetched_at")[:200]:
        seen.setdefault(f"{snapshot.base_id}/{snapshot.quote_id}",
                        str(snapshot.effective_rate))
    return seen


class FeeSettings(APIView):
    permission_classes = [CanWriteSettings]

    def get(self, request):
        return Response(FeeRuleSerializer(
            FeeRule.objects.filter(is_active=True).select_related(
                "corridor__source", "corridor__target", "country", "fee_currency"),
            many=True,
        ).data)

    def put(self, request):
        """Changing a fee writes a new rule and retires the old one.

        Editing the row in place would rewrite the terms of transfers that are
        still open. A new rule with a fresh valid_from leaves history intact.
        """
        rule_id = request.data.get("id")
        existing = FeeRule.objects.filter(pk=rule_id).first() if rule_id else None
        form = FeeRuleSerializer(data=request.data)
        form.is_valid(raise_exception=True)

        if existing is not None:
            existing.valid_to = timezone.now()
            existing.is_active = False
            existing.save(update_fields=["valid_to", "is_active"])

        rule = form.save(valid_from=timezone.now())
        audit.record(
            actor=request.user, action="settings.fee_changed",
            summary=(
                f"{request.user.email} changed the fee "
                f"{('from ' + str(existing.percent) + '% ') if existing else ''}"
                f"to {rule.percent}%"
                f"{' for ' + rule.country.name if rule.country else ''}"
            ),
            target=rule,
            before={"percent": str(existing.percent)} if existing else {},
            after={"percent": str(rule.percent)}, request=request,
        )
        return Response(FeeRuleSerializer(rule).data, status=status.HTTP_201_CREATED)


class LimitSettings(APIView):
    permission_classes = [CanWriteSettings]

    def get(self, request):
        return Response(TransferLimitSerializer(
            TransferLimit.objects.select_related(
                "corridor__source", "corridor__target", "currency"),
            many=True,
        ).data)

    def put(self, request):
        limit = generics.get_object_or_404(TransferLimit, pk=request.data.get("id"))
        before = TransferLimitSerializer(limit).data
        form = TransferLimitSerializer(limit, data=request.data, partial=True)
        form.is_valid(raise_exception=True)
        form.save()
        audit.record(actor=request.user, action="settings.limits_changed",
                     summary=f"{request.user.email} changed transfer limits on {limit.corridor}",
                     target=limit, before=before, after=form.data, request=request)
        return Response(form.data)


class CompanySettings(APIView):
    permission_classes = [CanWriteSettings]

    def get(self, request):
        return Response({
            "company": PlatformSetting.get("company"),
            "security": PlatformSetting.get("security"),
            "fraud": PlatformSetting.get("fraud"),
        })

    def put(self, request):
        for key in ("company", "security", "fraud"):
            if key not in request.data:
                continue
            row, _ = PlatformSetting.objects.get_or_create(
                key=key, defaults={"value": PlatformSetting.DEFAULTS.get(key, {})}
            )
            before = row.value
            merged = {**PlatformSetting.DEFAULTS.get(key, {}), **before, **request.data[key]}

            # Two-factor cannot be switched off for accounts that move money.
            if key == "security":
                merged["admin_2fa_required"] = True

            row.value = merged
            row.updated_by = request.user
            row.save()
            audit.record(actor=request.user, action=f"settings.{key}_changed",
                         summary=f"{request.user.email} updated {key} settings",
                         target=row, before=before, after=merged, request=request)
        return self.get(request)


class AdminPaymentMethods(APIView):
    permission_classes = [CanWriteSettings]

    def get(self, request):
        methods = PaymentMethod.objects.select_related("instruction", "country")
        return Response(AdminPaymentMethodSerializer(methods, many=True).data)

    def put(self, request, pk=None):
        method = generics.get_object_or_404(PaymentMethod, pk=pk or request.data.get("id"))
        before = {"is_enabled": method.is_enabled, "label": method.label}

        if "is_enabled" in request.data:
            method.is_enabled = bool(request.data["is_enabled"])
        for field in ("label", "note", "icon", "sort_order"):
            if field in request.data:
                setattr(method, field, request.data[field])
        method.save()

        if "fields" in request.data or "body" in request.data:
            instruction, _ = PaymentInstruction.objects.get_or_create(method=method)
            if "fields" in request.data:
                instruction.fields = request.data["fields"]
            if "body" in request.data:
                instruction.body = request.data["body"]
            if "reference_format" in request.data:
                instruction.reference_format = request.data["reference_format"]
            instruction.updated_by = request.user
            instruction.save()

        audit.record(actor=request.user, action="settings.payment_method_changed",
                     summary=(f"{request.user.email} updated {method.label} "
                              f"({'on' if method.is_enabled else 'off'})"),
                     target=method, before=before,
                     after={"is_enabled": method.is_enabled}, request=request)
        return Response(AdminPaymentMethodSerializer(method).data)


class AdminCountries(APIView):
    permission_classes = [CanWriteSettings]

    def get(self, request):
        return Response({
            "countries": CountrySerializer(
                Country.objects.select_related("currency"), many=True).data,
            "corridors": CorridorSerializer(
                Corridor.objects.select_related("source__currency", "target__currency"),
                many=True).data,
        })

    def put(self, request, iso2=None):
        country = generics.get_object_or_404(Country, pk=(iso2 or "").upper())
        before = {"is_enabled": country.is_enabled}

        if request.data.get("is_enabled") is False:
            # Closing a corridor under an open transfer strands the customer
            # mid-flow, so it is refused rather than warned about.
            open_count = Transaction.objects.open().filter(
                Q(corridor__source=country) | Q(corridor__target=country)
            ).count()
            if open_count:
                raise DomainError(
                    "country_in_use",
                    f"{country.name} has {open_count} open transfers. "
                    f"Close them before switching it off.",
                )

        for field in ("is_enabled", "is_origin", "is_destination", "sort_order"):
            if field in request.data:
                setattr(country, field, request.data[field])
        country.save()

        audit.record(actor=request.user, action="settings.country_changed",
                     summary=(f"{request.user.email} turned {country.name} "
                              f"{'on' if country.is_enabled else 'off'}"),
                     target=country, before=before,
                     after={"is_enabled": country.is_enabled}, request=request)
        return Response(CountrySerializer(country).data)


# --- News, disputes, notifications, audit --------------------------------


class AdminNewsList(generics.ListCreateAPIView):
    permission_classes = [CanChat]
    serializer_class = AdminNewsSerializer
    queryset = NewsPost.objects.all()

    def perform_create(self, serializer):
        post = serializer.save(author=self.request.user)
        if post.is_published and post.publish_at is None:
            post.publish_at = timezone.now()
            post.save(update_fields=["publish_at"])
        audit.record(actor=self.request.user, action="news.created",
                     summary=f"{self.request.user.email} created the article {post.title!r}",
                     target=post, request=self.request)


class AdminNewsDetail(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [CanChat]
    serializer_class = AdminNewsSerializer
    queryset = NewsPost.objects.all()

    def perform_update(self, serializer):
        was_published = serializer.instance.is_published
        post = serializer.save()
        if post.is_published and post.publish_at is None:
            post.publish_at = timezone.now()
            post.save(update_fields=["publish_at"])
        if post.is_published and not was_published:
            audit.record(actor=self.request.user, action="news.published",
                         summary=f"{self.request.user.email} published {post.title!r}",
                         target=post, request=self.request)


class AdminDisputeList(generics.ListAPIView):
    permission_classes = [IsDesk]
    serializer_class = DisputeSerializer

    def get_queryset(self):
        queryset = Dispute.objects.select_related(
            "transaction__user__profile", "transaction__send_currency",
            "transaction__receive_currency", "raised_by",
        )
        state = self.request.query_params.get("state")
        if state:
            queryset = queryset.filter(state=state)
        return queryset

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        # The resolutions travel with the list so the screen offers exactly the
        # ones the resolve endpoint will accept. A hardcoded copy in the front
        # end is a copy that drifts.
        response.data["resolutions"] = [
            {"value": value, "label": label} for value, label in Dispute.RESOLUTIONS
        ]
        response.data["counts"] = {
            "open": Dispute.objects.filter(state=Dispute.OPEN).count(),
            "resolved": Dispute.objects.filter(state=Dispute.RESOLVED).count(),
            "escalated": Dispute.objects.filter(state=Dispute.ESCALATED).count(),
        }
        return response


class AdminDisputeResolve(APIView):
    permission_classes = [CanMoveMoney]

    def post(self, request, pk):
        from nkenzapay.transactions.models import MessageKind

        dispute = generics.get_object_or_404(Dispute, pk=pk)
        resolution = request.data.get("resolution")
        note = request.data.get("note", "").strip()
        if resolution not in dict(Dispute.RESOLUTIONS):
            raise DomainError("bad_resolution", "Pick one of the listed resolutions.")

        txn = dispute.transaction
        dispute.resolution = resolution
        dispute.resolution_note = note
        dispute.resolved_by = request.user
        dispute.resolved_at = timezone.now()
        dispute.state = Dispute.ESCALATED if resolution == "escalated" else Dispute.RESOLVED
        dispute.save()

        # Each resolution maps to a status move, so the case and the transfer
        # never disagree about where things stand.
        if resolution == "closed":
            txn_services.confirm_received(reference=txn.reference, user=txn.user,
                                          request=request)
        elif resolution == "resent":
            txn_services._advance(  # noqa: SLF001 - the service module owns this move
                txn, Status.PAYOUT_PROCESSING, actor=request.user, request=request,
                audit_action="dispute.payout_resent",
                audit_summary=f"{request.user.email} is resending the payout on {txn.reference}",
            )
        elif resolution == "refunded":
            txn_services._advance(  # noqa: SLF001
                txn, Status.REFUND_PENDING, actor=request.user, request=request,
                audit_action="dispute.refund_started",
                audit_summary=f"{request.user.email} started a refund on {txn.reference}",
            )
        else:
            audit.record(actor=request.user, action="dispute.escalated",
                         summary=f"{request.user.email} escalated {txn.reference}",
                         target=dispute, request=request)

        Message.objects.create(
            transaction=txn, sender=request.user, is_from_desk=True,
            kind=MessageKind.ACTION,
            body=dict(Dispute.RESOLUTIONS)[resolution],
            payload={"action": "dispute_resolved", "note": note, "icon": "gavel"},
        )
        return Response(DisputeSerializer(dispute).data)


class AdminNotifications(generics.ListAPIView):
    permission_classes = [IsDesk]

    def get_serializer_class(self):
        from nkenzapay.notifications.serializers import NotificationSerializer

        return NotificationSerializer

    def get_queryset(self):
        return Notification.objects.filter(user=self.request.user,
                                           audience=Notification.ADMIN)


class AdminDeliveryRules(APIView):
    permission_classes = [CanWriteSettings]

    def get(self, request):
        rules = DeliveryRule.objects.all()
        return Response([
            {"event": r.event, "label": r.label, "email_admins": r.email_admins,
             "email_customer": r.email_customer}
            for r in rules
        ])

    def put(self, request):
        rule = generics.get_object_or_404(DeliveryRule, event=request.data.get("event"))
        for field in ("email_admins", "email_customer"):
            if field in request.data:
                setattr(rule, field, bool(request.data[field]))
        rule.save()
        audit.record(actor=request.user, action="settings.notification_rule_changed",
                     summary=f"{request.user.email} changed email delivery for {rule.label}",
                     target=rule, request=request)
        return Response({"event": rule.event, "email_admins": rule.email_admins,
                         "email_customer": rule.email_customer})


class AdminAudit(generics.ListAPIView):
    permission_classes = [IsDesk]
    serializer_class = AuditEntrySerializer

    def get_queryset(self):
        queryset = AuditEntry.objects.select_related("actor")
        params = self.request.query_params
        if params.get("actor"):
            queryset = queryset.filter(actor_id=params["actor"])
        if params.get("action"):
            queryset = queryset.filter(action__startswith=params["action"])
        if params.get("q"):
            queryset = queryset.filter(summary__icontains=params["q"])
        if params.get("from"):
            queryset = queryset.filter(at__date__gte=params["from"])
        if params.get("to"):
            queryset = queryset.filter(at__date__lte=params["to"])
        return queryset


class AdminAccounts(APIView):
    permission_classes = [CanWriteSettings]

    def get(self, request):
        return Response(AdminUserSerializer(
            AdminUser.objects.select_related("user"), many=True).data)


# --- Analytics and exports ------------------------------------------------


class Analytics(APIView):
    permission_classes = [IsDesk]

    def get(self, request, family):
        since, until = parse_range(request)
        builder = {
            "website": self.website,
            "users": self.users,
            "transactions": self.transactions,
            "financial": self.financial,
        }.get(family)
        if builder is None:
            raise DomainError("unknown_family", "That analytics family does not exist.")
        return Response(builder(since, until))

    def website(self, since, until):
        views = PageView.objects.filter(at__range=(since, until))
        total = views.count()
        sessions = views.values("session_key").distinct().count()
        top_pages = list(
            views.values("path").annotate(count=Count("id")).order_by("-count")[:5]
        )
        sources = list(
            views.values("source").annotate(count=Count("id")).order_by("-count")[:5]
        )
        devices = list(views.values("device").annotate(count=Count("id")))
        return {
            "total_visits": sessions,
            "unique_visitors": views.values("session_key").distinct().count(),
            "page_views": total,
            "views_per_visit": round(total / sessions, 2) if sessions else 0,
            "top_pages": top_pages,
            "sources": sources,
            "devices": devices,
        }

    def users(self, since, until):
        return {
            "registrations": User.objects.count(),
            "new_registrations": User.objects.filter(
                date_joined__range=(since, until)).count(),
            "active": User.objects.filter(
                last_seen_at__range=(since, until)).count(),
            "sign_ins": LoginActivity.objects.filter(
                at__range=(since, until), succeeded=True).count(),
            "series": list(
                User.objects.filter(date_joined__range=(since, until))
                .annotate(day=TruncDate("date_joined"))
                .values("day").annotate(count=Count("id")).order_by("day")
            ),
        }

    def transactions(self, since, until):
        rows = Transaction.objects.filter(created_at__range=(since, until))
        total = rows.count() or 1
        by_status = list(rows.values("status").annotate(count=Count("id")))
        return {
            "receives": rows.filter(direction="receive").count(),
            "sends": rows.filter(direction="send").count(),
            "completed": rows.filter(status=Status.COMPLETED).count(),
            "disputed": rows.filter(status=Status.DISPUTED).count(),
            "completion_rate": round(
                rows.filter(status=Status.COMPLETED).count() * 100 / total, 1),
            "by_status": by_status,
            "average_value": str(rows.aggregate(v=Avg("send_amount"))["v"] or 0),
        }

    def financial(self, since, until):
        rows = Transaction.objects.filter(created_at__range=(since, until))
        return {
            "xaf_processed": str(rows.filter(send_currency="XAF").aggregate(
                t=Sum("send_amount"))["t"] or 0),
            "inr_processed": str(rows.filter(send_currency="INR").aggregate(
                t=Sum("send_amount"))["t"] or 0),
            "fees": str(rows.aggregate(t=Sum("fee_amount"))["t"] or 0),
            "average_transfer": str(rows.aggregate(v=Avg("send_amount"))["v"] or 0),
            "by_method": list(
                rows.values("collect_method__label").annotate(
                    count=Count("id"), volume=Sum("send_amount")).order_by("-count")
            ),
            "by_country": list(
                rows.values("corridor__source__name").annotate(
                    count=Count("id"), volume=Sum("send_amount")).order_by("-count")
            ),
        }


class ExportCreate(generics.ListCreateAPIView):
    permission_classes = [IsDesk]
    serializer_class = ExportJobSerializer

    def get_queryset(self):
        return ExportJob.objects.filter(requested_by=self.request.user)

    def perform_create(self, serializer):
        job = serializer.save(requested_by=self.request.user)
        from nkenzapay.analytics.exports import run_export

        run_export(job.pk)
        audit.record(actor=self.request.user, action="export.requested",
                     summary=(f"{self.request.user.email} exported "
                              f"{', '.join(job.datasets)} as {job.fmt}"),
                     target=job, request=self.request)


class ExportDetail(generics.RetrieveAPIView):
    permission_classes = [IsDesk]
    serializer_class = ExportJobSerializer

    def get_queryset(self):
        return ExportJob.objects.filter(requested_by=self.request.user)


@api_view(["GET"])
@permission_classes([IsDesk])
def export_download(request, pk):
    from django.http import FileResponse, Http404
    import io

    from nkenzapay.common.storage import storage

    job = generics.get_object_or_404(ExportJob, pk=pk, requested_by=request.user)
    if job.state != ExportJob.READY or not job.storage_key:
        raise DomainError("not_ready", "That export is still being built.")
    try:
        data = storage().read_bytes(job.storage_key)
    except (FileNotFoundError, OSError) as exc:
        raise Http404 from exc
    extension = "xlsx" if job.fmt == "excel" else "csv"
    return FileResponse(
        io.BytesIO(data), as_attachment=True,
        filename=f"nkenzapay-export-{job.pk}.{extension}",
    )
