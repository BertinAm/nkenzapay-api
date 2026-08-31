from rest_framework import serializers

from .models import Notification, NotificationPreference


class NotificationSerializer(serializers.ModelSerializer):
    reference = serializers.SerializerMethodField()
    category = serializers.CharField(read_only=True)
    is_unread = serializers.BooleanField(read_only=True)

    class Meta:
        model = Notification
        fields = ["id", "event", "category", "title", "body", "icon", "tone",
                  "action", "reference", "is_unread", "read_at", "created_at"]

    def get_reference(self, obj):
        return obj.transaction.reference if obj.transaction_id else None


class NotificationPreferenceSerializer(serializers.ModelSerializer):
    label = serializers.SerializerMethodField()

    class Meta:
        model = NotificationPreference
        fields = ["channel_group", "label", "in_app", "email", "push", "is_locked"]

    def get_label(self, obj):
        return dict((g, lbl) for g, lbl, _ in NotificationPreference.GROUPS).get(
            obj.channel_group, obj.channel_group
        )
