from decimal import Decimal, InvalidOperation

from rest_framework import serializers

from nkenzapay.common.money import display_amount, format_amount
from nkenzapay.geo.models import Corridor

from .models import Quote


class QuoteRequestSerializer(serializers.Serializer):
    corridor = serializers.IntegerField(required=False)
    source = serializers.CharField(required=False, max_length=2)
    target = serializers.CharField(required=False, max_length=2)
    direction = serializers.ChoiceField(choices=["receive", "send"])
    send_amount = serializers.CharField()

    def validate_send_amount(self, value):
        cleaned = str(value).replace(",", "").replace(" ", "").strip()
        try:
            amount = Decimal(cleaned)
        except (InvalidOperation, ValueError) as exc:
            raise serializers.ValidationError("Enter an amount.") from exc
        if amount <= 0:
            raise serializers.ValidationError("Enter an amount greater than zero.")
        if amount > Decimal("1000000000"):
            raise serializers.ValidationError("That amount is larger than the platform handles.")
        return amount

    def validate(self, attrs):
        corridor = None
        if attrs.get("corridor"):
            corridor = Corridor.objects.filter(pk=attrs["corridor"], is_enabled=True).first()
        elif attrs.get("source") and attrs.get("target"):
            corridor = Corridor.objects.filter(
                source_id=attrs["source"].upper(),
                target_id=attrs["target"].upper(),
                is_enabled=True,
            ).first()
        if corridor is None:
            raise serializers.ValidationError(
                {"corridor": "That corridor is not open yet."}
            )
        attrs["corridor_obj"] = corridor
        return attrs


class QuoteResultSerializer(serializers.Serializer):
    """The shape the calculator renders.

    Both a machine value and a display string go out for every figure. The
    front end must never format money itself — grouping differs by currency and
    getting it wrong on a receipt is the kind of bug customers remember.
    """

    reference = serializers.SerializerMethodField()
    direction = serializers.CharField()
    corridor = serializers.SerializerMethodField()
    send_currency = serializers.SerializerMethodField()
    receive_currency = serializers.SerializerMethodField()
    send_amount = serializers.SerializerMethodField()
    converted_amount = serializers.SerializerMethodField()
    fee_percent = serializers.DecimalField(max_digits=5, decimal_places=2)
    fee_amount = serializers.SerializerMethodField()
    receive_amount = serializers.SerializerMethodField()
    rate_used = serializers.SerializerMethodField()
    expires_at = serializers.DateTimeField()
    seconds_remaining = serializers.SerializerMethodField()
    limits = serializers.SerializerMethodField()
    errors = serializers.ListField(required=False)
    needs_manual_review = serializers.BooleanField(required=False)

    def get_reference(self, obj):
        return getattr(obj, "reference", None)

    def get_corridor(self, obj):
        return {
            "id": obj.corridor.pk,
            "source": obj.corridor.source.iso2,
            "target": obj.corridor.target.iso2,
            "source_name": obj.corridor.source.name,
            "target_name": obj.corridor.target.name,
            "source_flag": obj.corridor.source.flag_emoji,
            "target_flag": obj.corridor.target.flag_emoji,
        }

    def get_send_currency(self, obj):
        return _currency(obj.send_currency)

    def get_receive_currency(self, obj):
        return _currency(obj.receive_currency)

    def get_send_amount(self, obj):
        return _money(obj.send_amount, obj.send_currency)

    def get_converted_amount(self, obj):
        return _money(obj.converted_amount, obj.receive_currency)

    def get_fee_amount(self, obj):
        return _money(obj.fee_amount, obj.receive_currency)

    def get_receive_amount(self, obj):
        return _money(obj.receive_amount, obj.receive_currency)

    def get_rate_used(self, obj):
        rate = obj.rate_used.normalize()
        return {
            "value": str(obj.rate_used),
            "display": f"1 {obj.send_currency.code} = {rate:f} {obj.receive_currency.code}",
        }

    def get_seconds_remaining(self, obj):
        from django.utils import timezone

        return max(0, int((obj.expires_at - timezone.now()).total_seconds()))

    def get_limits(self, obj):
        limit = getattr(obj, "limit", None)
        if limit is None:
            return None

        # format_limit, not format_amount: a bound is a round figure and reads
        # like one. "Minimum 1,000.00 INR" is a database column talking.
        from nkenzapay.pricing.engine import format_limit

        code = obj.send_currency.code
        return {
            "minimum": str(limit.minimum),
            "minimum_display": format_limit(limit.minimum, code),
            "maximum": str(limit.maximum) if limit.maximum is not None else None,
            "maximum_display": (
                format_limit(limit.maximum, code) if limit.maximum is not None else None
            ),
            "currency": code,
        }


def _currency(currency):
    return {
        "code": currency.code,
        "symbol": currency.symbol,
        "minor_units": currency.minor_units,
    }


def _money(amount, currency):
    return {
        "value": str(amount),
        "display": display_amount(amount, currency.code),
        "plain": format_amount(amount, currency.code),
        "currency": currency.code,
    }


class QuoteSerializer(QuoteResultSerializer):
    class Meta:
        model = Quote
