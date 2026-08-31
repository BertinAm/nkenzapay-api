"""Security endpoints and handlers."""
from datetime import timedelta

from django.db.models import Count, Max
from django.http import JsonResponse
from django.utils import timezone
from rest_framework import generics
from rest_framework.response import Response
from rest_framework.views import APIView

from nkenzapay.adminapi.permissions import CanWriteSettings, IsDesk
from nkenzapay.audit import services as audit
from nkenzapay.common.exceptions import DomainError

from . import services
from .models import BlockedAddress, EventKind, SecurityEvent, Severity
from .serializers import BlockedAddressSerializer, SecurityEventSerializer


def csrf_failure(request, reason=""):
    """Django's CSRF failure view, replaced so the rejection is recorded.

    A genuine failure is usually a stale tab. A burst of them from one address
    is someone trying to forge requests, and the difference only shows in the
    pattern.
    """
    services.record(
        EventKind.CSRF_FAILED,
        request=request,
        summary="CSRF verification failed",
        detail={"reason": reason[:200]},
        status_code=403,
    )
    return JsonResponse(
        {
            "error": {
                "code": "csrf_failed",
                "message": "Your session expired. Reload the page and try again.",
                "detail": {},
            }
        },
        status=403,
    )


class SecurityOverview(APIView):
    """The numbers the desk's security screen leads with."""

    permission_classes = [IsDesk]

    def get(self, request):
        hours = int(request.query_params.get("hours", 24))
        since = timezone.now() - timedelta(hours=hours)
        events = SecurityEvent.objects.filter(at__gte=since)

        by_kind = list(
            events.values("kind", "severity")
            .annotate(count=Count("id"))
            .order_by("-count")[:12]
        )
        for row in by_kind:
            row["label"] = EventKind(row["kind"]).label

        top_addresses = list(
            events.exclude(ip__isnull=True)
            .values("ip")
            .annotate(count=Count("id"), last_seen=Max("at"))
            .order_by("-count")[:10]
        )
        blocked = set(
            BlockedAddress.objects.filter(
                ip__in=[row["ip"] for row in top_addresses]
            ).values_list("ip", flat=True)
        )
        for row in top_addresses:
            row["is_blocked"] = row["ip"] in blocked

        return Response({
            "window_hours": hours,
            "totals": {
                "events": events.count(),
                "critical": events.filter(severity=Severity.CRITICAL).count(),
                "high": events.filter(severity=Severity.HIGH).count(),
                "blocked_now": BlockedAddress.objects.count(),
                "failed_logins": events.filter(kind=EventKind.LOGIN_FAILED).count(),
                "probes": events.filter(
                    kind__in=[EventKind.INJECTION_PROBE, EventKind.TRAVERSAL_PROBE]
                ).count(),
                "scanners": events.filter(kind=EventKind.SCANNER).count(),
            },
            "by_kind": by_kind,
            "top_addresses": top_addresses,
            "series": self.series(since),
        })

    def series(self, since):
        """Hourly counts, so a spike is visible rather than averaged away.

        Every hour in the window gets a bucket, including the empty ones. A
        chart built only from the hours that had events draws one busy hour as
        a full-width block, which reads as "constant attack" when it means the
        opposite.
        """
        now = timezone.now()
        start = since.replace(minute=0, second=0, microsecond=0)
        hours = max(1, min(int((now - start).total_seconds() // 3600) + 1, 720))

        buckets = {
            (start + timedelta(hours=offset)).strftime("%Y-%m-%dT%H:00"): 0
            for offset in range(hours)
        }
        for at in SecurityEvent.objects.filter(at__gte=start).values_list("at", flat=True):
            key = at.strftime("%Y-%m-%dT%H:00")
            if key in buckets:
                buckets[key] += 1

        return [{"hour": k, "count": v} for k, v in sorted(buckets.items())]


class SecurityEventList(generics.ListAPIView):
    permission_classes = [IsDesk]
    serializer_class = SecurityEventSerializer

    def get_queryset(self):
        queryset = SecurityEvent.objects.select_related("user")
        params = self.request.query_params

        if params.get("kind"):
            queryset = queryset.filter(kind__in=params["kind"].split(","))
        if params.get("severity"):
            queryset = queryset.filter(severity__in=params["severity"].split(","))
        if params.get("ip"):
            queryset = queryset.filter(ip=params["ip"])
        if params.get("q"):
            queryset = queryset.filter(summary__icontains=params["q"])
        if params.get("hours"):
            since = timezone.now() - timedelta(hours=int(params["hours"]))
            queryset = queryset.filter(at__gte=since)
        return queryset


class SecurityEventDetail(generics.RetrieveAPIView):
    permission_classes = [IsDesk]
    serializer_class = SecurityEventSerializer
    queryset = SecurityEvent.objects.select_related("user")


class BlockedAddressList(generics.ListAPIView):
    permission_classes = [IsDesk]
    serializer_class = BlockedAddressSerializer
    queryset = BlockedAddress.objects.select_related("blocked_by")


class BlockAddress(APIView):
    """Blocking is a settings-level action: it can lock out real customers."""

    permission_classes = [CanWriteSettings]

    def post(self, request):
        ip = (request.data.get("ip") or "").strip()
        reason = (request.data.get("reason") or "").strip()
        hours = request.data.get("hours")

        if not ip:
            raise DomainError("ip_required", "Which address should be blocked?")
        if not reason:
            raise DomainError("reason_required", "A block needs a reason.")

        entry = services.block(
            ip,
            reason=reason,
            duration=timedelta(hours=int(hours)) if hours else None,
            actor=request.user,
        )
        audit.record(
            actor=request.user,
            action="security.address_blocked",
            summary=f"{request.user.email} blocked {ip}: {reason[:120]}",
            target=entry,
            request=request,
        )
        return Response(BlockedAddressSerializer(entry).data, status=201)

    def delete(self, request):
        ip = (request.data.get("ip") or request.query_params.get("ip") or "").strip()
        if not ip:
            raise DomainError("ip_required", "Which address should be unblocked?")

        services.unblock(ip, actor=request.user)
        audit.record(
            actor=request.user,
            action="security.address_unblocked",
            summary=f"{request.user.email} unblocked {ip}",
            request=request,
        )
        return Response(status=204)
