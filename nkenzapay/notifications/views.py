from django.utils import timezone
from rest_framework import generics
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Notification, NotificationPreference
from .serializers import NotificationPreferenceSerializer, NotificationSerializer


class NotificationList(generics.ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = NotificationSerializer

    def get_queryset(self):
        audience = (
            Notification.ADMIN
            if self.request.query_params.get("audience") == "admin"
            and self.request.user.is_desk
            else Notification.CUSTOMER
        )
        queryset = Notification.objects.filter(user=self.request.user, audience=audience)
        wanted = self.request.query_params.get("filter")
        if wanted and wanted != "all":
            if wanted == "unread":
                queryset = queryset.filter(read_at__isnull=True)
            else:
                queryset = [n for n in queryset if n.category == wanted]
        return queryset

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        response.data["unread"] = Notification.objects.filter(
            user=request.user, read_at__isnull=True
        ).count()
        return response


class MarkRead(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        ids = request.data.get("ids")
        queryset = Notification.objects.filter(user=request.user, read_at__isnull=True)
        if ids:
            queryset = queryset.filter(id__in=ids)
        count = queryset.update(read_at=timezone.now())
        return Response({"marked": count})


class PreferencesView(APIView):
    """Locked groups reject changes rather than silently ignoring them, so a
    toggle that cannot move says why."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .services import seed_preferences

        seed_preferences(request.user)
        rows = NotificationPreference.objects.filter(user=request.user)
        return Response(NotificationPreferenceSerializer(rows, many=True).data)

    def patch(self, request):
        from nkenzapay.common.exceptions import DomainError

        group = request.data.get("channel_group")
        row = NotificationPreference.objects.filter(
            user=request.user, channel_group=group
        ).first()
        if row is None:
            raise DomainError("unknown_group", "That notification group does not exist.")
        if row.is_locked:
            raise DomainError(
                "locked_group",
                "Transfer messages always arrive. This one cannot be switched off.",
            )
        for field in ("in_app", "email", "push"):
            if field in request.data:
                setattr(row, field, bool(request.data[field]))
        row.save()
        return Response(NotificationPreferenceSerializer(row).data)
