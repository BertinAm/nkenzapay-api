from rest_framework import serializers

from nkenzapay.accounts.models import AdminUser, User
from nkenzapay.analytics.models import ExportJob
from nkenzapay.audit.models import AuditEntry
from nkenzapay.common.money import display_amount
from nkenzapay.disputes.models import Dispute
from nkenzapay.pricing.models import FeeRule, TransferLimit
from nkenzapay.rates.models import RateProvider
from nkenzapay.transactions.serializers import TransactionListSerializer


class AdminTransactionListSerializer(TransactionListSerializer):
    """The desk's table, which needs a name against every row.

    A customer's own list carries no name because they know who they are. The
    desk is looking at everybody's.
    """

    customer = serializers.CharField(source="user.display_name", read_only=True)
    initials = serializers.CharField(source="user.initials", read_only=True)
    waiting_minutes = serializers.SerializerMethodField()

    class Meta(TransactionListSerializer.Meta):
        fields = TransactionListSerializer.Meta.fields + [
            "customer", "initials", "waiting_minutes",
        ]

    def get_waiting_minutes(self, obj):
        """How long this row has been sitting on the desk.

        Null unless the desk is what it is waiting for: a completed transfer
        has not been waiting for anybody, and a number there would read as an
        SLA breach that never happened.

        Reads the `status_since` annotation where the queryset supplies one and
        falls back to the last status change, so the value is right whether the
        serializer is handed an annotated list or a plain one.
        """
        from django.utils import timezone

        from nkenzapay.transactions.models import Status

        if obj.status not in (Status.PROOF_SUBMITTED, Status.PAYMENT_VERIFICATION):
            return None

        since = getattr(obj, "status_since", None)
        if since is None:
            last = obj.history.all().last()
            since = last.at if last else obj.created_at
        return int((timezone.now() - since).total_seconds() // 60)


class RateProviderSerializer(serializers.ModelSerializer):
    is_healthy = serializers.BooleanField(read_only=True)

    class Meta:
        model = RateProvider
        fields = ["id", "slug", "label", "is_active", "refresh_seconds", "hold_seconds",
                  "markup_bps", "last_success_at", "last_error", "is_healthy"]
        read_only_fields = ["last_success_at", "last_error"]

    def validate_markup_bps(self, value):
        # A markup over five percent stops being a spread and starts being a
        # second, undisclosed fee.
        if value > 500:
            raise serializers.ValidationError("A markup above 5% needs a different mechanism.")
        return value


class FeeRuleSerializer(serializers.ModelSerializer):
    scope = serializers.SerializerMethodField()

    class Meta:
        model = FeeRule
        fields = ["id", "corridor", "country", "direction", "percent", "min_fee",
                  "max_fee", "fee_currency", "is_active", "valid_from", "valid_to", "scope"]
        read_only_fields = ["valid_from", "valid_to"]

    def get_scope(self, obj):
        if obj.corridor_id:
            return str(obj.corridor)
        if obj.country_id:
            return obj.country.name
        return "All countries"

    def validate_percent(self, value):
        if value < 0 or value > 30:
            raise serializers.ValidationError("A fee should sit between 0% and 30%.")
        return value


class TransferLimitSerializer(serializers.ModelSerializer):
    corridor_label = serializers.SerializerMethodField()
    minimum_display = serializers.SerializerMethodField()

    class Meta:
        model = TransferLimit
        fields = ["id", "corridor", "corridor_label", "direction", "currency",
                  "minimum", "minimum_display", "maximum", "daily_maximum",
                  "monthly_maximum", "manual_review_above"]

    def get_corridor_label(self, obj):
        return f"{obj.corridor.source.name} to {obj.corridor.target.name}"

    def get_minimum_display(self, obj):
        return display_amount(obj.minimum, obj.currency_id)

    def validate(self, attrs):
        minimum = attrs.get("minimum", getattr(self.instance, "minimum", None))
        maximum = attrs.get("maximum", getattr(self.instance, "maximum", None))
        if minimum is not None and maximum is not None and maximum < minimum:
            raise serializers.ValidationError(
                {"maximum": "The maximum cannot sit below the minimum."}
            )
        return attrs


class CustomerListSerializer(serializers.ModelSerializer):
    name = serializers.CharField(source="display_name", read_only=True)
    initials = serializers.CharField(read_only=True)
    whatsapp = serializers.SerializerMethodField()
    transfer_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = User
        fields = ["id", "name", "initials", "email", "whatsapp", "transfer_count",
                  "date_joined", "is_suspended", "last_seen_at"]

    def get_whatsapp(self, obj):
        profile = getattr(obj, "profile", None)
        return profile.whatsapp_display if profile else ""


class CustomerDetailSerializer(CustomerListSerializer):
    photo_url = serializers.SerializerMethodField()
    photo_taken_at = serializers.DateTimeField(source="profile.photo_taken_at",
                                               read_only=True)
    country = serializers.CharField(source="profile.country_id", read_only=True)
    stats = serializers.SerializerMethodField()

    class Meta(CustomerListSerializer.Meta):
        fields = CustomerListSerializer.Meta.fields + [
            "photo_url", "photo_taken_at", "country", "suspended_reason", "stats",
        ]

    def get_photo_url(self, obj):
        profile = getattr(obj, "profile", None)
        if not profile or not profile.photo_key:
            return None
        from nkenzapay.common.storage import storage

        return storage().presign_get(profile.photo_key, ttl=300)

    def get_stats(self, obj):
        from django.db.models import Count, Sum

        from nkenzapay.transactions.models import Status, Transaction

        rows = Transaction.objects.filter(user=obj)
        totals = rows.filter(status=Status.COMPLETED).aggregate(
            volume=Sum("send_amount"), count=Count("id")
        )
        return {
            "transfers": rows.count(),
            "completed": totals["count"] or 0,
            "volume": str(totals["volume"] or 0),
            "disputes": Dispute.objects.filter(transaction__user=obj).count(),
        }


class ThreadSummarySerializer(serializers.Serializer):
    """One row in the desk inbox."""

    reference = serializers.CharField()
    customer = serializers.SerializerMethodField()
    initials = serializers.SerializerMethodField()
    snippet = serializers.SerializerMethodField()
    last_at = serializers.SerializerMethodField()
    unread = serializers.SerializerMethodField()
    status = serializers.CharField()
    short_status_label = serializers.CharField()
    direction = serializers.CharField()
    method = serializers.CharField(source="collect_method.label")

    def get_customer(self, obj):
        return obj.user.display_name

    def get_initials(self, obj):
        return obj.user.initials

    def get_snippet(self, obj):
        last = obj.messages.all().last()
        if last is None:
            return ""
        if last.body:
            return last.body[:120]
        return "Attachment" if last.kind == "attachment" else last.get_kind_display()

    def get_last_at(self, obj):
        last = obj.messages.all().last()
        return last.created_at if last else obj.created_at

    def get_unread(self, obj):
        return sum(
            1 for m in obj.messages.all()
            if m.read_at is None and not m.is_from_desk
        )


class DisputeSerializer(serializers.ModelSerializer):
    reference = serializers.CharField(source="transaction.reference", read_only=True)
    customer = serializers.SerializerMethodField()
    initials = serializers.SerializerMethodField()
    amount = serializers.SerializerMethodField()
    reason = serializers.CharField(source="reason_display", read_only=True)
    age_days = serializers.SerializerMethodField()
    method = serializers.CharField(source="transaction.collect_method.label",
                                   read_only=True, default="")
    # When the desk said it had paid. On a "money never arrived" case this is
    # the first thing anyone asks for, and it is the difference between a
    # payout that is late and one that was never made.
    payout_sent_at = serializers.DateTimeField(source="transaction.payout_sent_at",
                                               read_only=True)

    class Meta:
        model = Dispute
        fields = ["id", "reference", "customer", "initials", "amount", "reason_code",
                  "reason", "detail", "state", "resolution", "resolution_note",
                  "created_at", "resolved_at", "age_days", "method", "payout_sent_at"]

    def get_customer(self, obj):
        return obj.transaction.user.display_name

    def get_initials(self, obj):
        return obj.transaction.user.initials

    def get_amount(self, obj):
        txn = obj.transaction
        return display_amount(txn.receive_amount, txn.receive_currency_id)

    def get_age_days(self, obj):
        from django.utils import timezone

        return (timezone.now() - obj.created_at).days


class AuditEntrySerializer(serializers.ModelSerializer):
    actor_name = serializers.SerializerMethodField()

    class Meta:
        model = AuditEntry
        fields = ["id", "at", "action", "summary", "actor_name", "target_type",
                  "target_id", "before", "after"]

    def get_actor_name(self, obj):
        return obj.actor.display_name if obj.actor else "System"


class AdminUserSerializer(serializers.ModelSerializer):
    name = serializers.CharField(source="user.display_name", read_only=True)
    email = serializers.CharField(source="user.email", read_only=True)
    initials = serializers.CharField(source="user.initials", read_only=True)
    has_2fa = serializers.SerializerMethodField()

    class Meta:
        model = AdminUser
        fields = ["id", "name", "email", "initials", "role", "has_2fa", "created_at"]

    def get_has_2fa(self, obj):
        return obj.totp_confirmed_at is not None


class ExportJobSerializer(serializers.ModelSerializer):
    download_url = serializers.SerializerMethodField()

    class Meta:
        model = ExportJob
        fields = ["id", "datasets", "filters", "date_from", "date_to", "fmt",
                  "state", "row_count", "error", "created_at", "finished_at",
                  "download_url"]
        read_only_fields = ["state", "row_count", "error", "finished_at"]

    def get_download_url(self, obj):
        if obj.state != ExportJob.READY:
            return None
        return f"/api/v1/admin/exports/{obj.pk}/download"

    def validate_datasets(self, value):
        from nkenzapay.analytics.exports import DATASETS

        unknown = [d for d in value if d not in DATASETS]
        if unknown:
            raise serializers.ValidationError(f"Unknown datasets: {', '.join(unknown)}")
        if not value:
            raise serializers.ValidationError("Pick at least one dataset to export.")
        return value
