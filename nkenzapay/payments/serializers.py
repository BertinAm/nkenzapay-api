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

    class Meta:
        model = PaymentInstruction
        fields = ["fields", "body", "qr_key", "reference_format", "rows", "updated_at"]

    def get_rows(self, obj):
        return obj.rows_for_chat(self.context.get("transaction"))


class AdminPaymentMethodSerializer(serializers.ModelSerializer):
    instruction = PaymentInstructionSerializer(read_only=True)
    summary = serializers.CharField(read_only=True)

    class Meta:
        model = PaymentMethod
        fields = ["id", "slug", "label", "country", "side", "icon", "note",
                  "is_enabled", "sort_order", "instruction", "summary"]
