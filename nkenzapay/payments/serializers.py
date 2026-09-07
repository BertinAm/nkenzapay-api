from rest_framework import serializers

from .models import PaymentInstruction, PaymentMethod


class PaymentMethodSerializer(serializers.ModelSerializer):
    """The public shape. Carries masked details only.

    Real account numbers appear once an order exists and the customer has
    something to pay against — before that they are only useful to someone
    scraping the site for an account to impersonate.
    """

    country = serializers.CharField(source="country_id")
    masked_details = serializers.SerializerMethodField()

    class Meta:
        model = PaymentMethod
        fields = ["id", "slug", "label", "country", "side", "icon", "note",
                  "is_enabled", "sort_order", "masked_details"]

    def get_masked_details(self, obj):
        instruction = getattr(obj, "instruction", None)
        if instruction is None:
            return []
        return [
            {"label": PaymentInstruction.LABELS.get(k, k.replace("_", " ").title()),
             "value": v}
            for k, v in instruction.masked_fields().items()
            if v
        ]


class PaymentInstructionSerializer(serializers.ModelSerializer):
    """The full detail, for a participant in a transaction."""

    rows = serializers.SerializerMethodField()
    labels = serializers.SerializerMethodField()
    hints = serializers.SerializerMethodField()

    class Meta:
        model = PaymentInstruction
        fields = ["fields", "labels", "hints", "body", "qr_key",
                  "reference_format", "rows", "updated_at"]

    def get_rows(self, obj):
        return obj.rows_for_chat(self.context.get("transaction"))

    def get_labels(self, obj):
        return {key: PaymentInstruction.LABELS.get(key, key.replace("_", " ").title())
                for key in obj.ordered_fields()}

    def get_hints(self, obj):
        return {key: PaymentInstruction.HINTS.get(key, "")
                for key in obj.ordered_fields()}

    def to_representation(self, instance):
        """Send every key the method expects, not only the ones already filled.

        A field set that grew after its row was written would otherwise never
        show its new boxes in the admin, and nobody would find out until a
        customer was told to pay into nothing.

        Done here rather than as a field named `fields`: that name belongs to
        the serializer itself, and declaring one shadows the machinery that
        builds every other field on it.
        """
        data = super().to_representation(instance)
        data["fields"] = instance.ordered_fields()
        return data


class AdminPaymentMethodSerializer(serializers.ModelSerializer):
    instruction = PaymentInstructionSerializer(read_only=True)
    summary = serializers.CharField(read_only=True)

    class Meta:
        model = PaymentMethod
        fields = ["id", "slug", "label", "country", "side", "icon", "note",
                  "is_enabled", "sort_order", "instruction", "summary"]
