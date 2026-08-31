from rest_framework import serializers

from .models import BlockedAddress, EventKind, SecurityEvent


class SecurityEventSerializer(serializers.ModelSerializer):
    kind_label = serializers.SerializerMethodField()
    who = serializers.SerializerMethodField()

    class Meta:
        model = SecurityEvent
        fields = ["id", "kind", "kind_label", "severity", "summary", "ip", "who",
                  "identifier", "method", "path", "status_code", "user_agent",
                  "referer", "country", "detail", "at"]

    def get_kind_label(self, obj):
        try:
            return EventKind(obj.kind).label
        except ValueError:
            return obj.kind

    def get_who(self, obj):
        if obj.user_id:
            return obj.user.display_name
        return obj.identifier or "Signed out"


class BlockedAddressSerializer(serializers.ModelSerializer):
    blocked_by_name = serializers.SerializerMethodField()
    is_active = serializers.BooleanField(read_only=True)
    is_permanent = serializers.BooleanField(read_only=True)

    class Meta:
        model = BlockedAddress
        fields = ["id", "ip", "reason", "kind", "hits", "is_automatic",
                  "blocked_by_name", "is_active", "is_permanent",
                  "expires_at", "created_at"]

    def get_blocked_by_name(self, obj):
        return obj.blocked_by.display_name if obj.blocked_by_id else "Automatic"
