from rest_framework import serializers

from nkenzapay.common.money import display_amount, format_amount
from nkenzapay.payments.models import PaymentMethod
from nkenzapay.rates.models import Quote

from .models import Attachment, Message, Receipt, Status, StatusHistory, Transaction

# The six steps the chat stepper shows. The thirteen statuses collapse into
# these because a customer does not need to know the difference between
# "payment confirmed" and "payout processing" — the desk does.
STEPPER = [
    ("Order created", [Status.ORDER_CREATED]),
    ("Awaiting payment", [Status.AWAITING_PAYMENT]),
    ("Proof submitted", [Status.PROOF_SUBMITTED]),
    ("Payment verification", [Status.PAYMENT_VERIFICATION]),
    ("Payout sent", [Status.PAYMENT_CONFIRMED, Status.PAYOUT_PROCESSING,
                     Status.PAYOUT_SENT, Status.AWAITING_CONFIRMATION]),
    ("Completed", [Status.COMPLETED]),
]


def money(amount, currency_code):
    return {
        "value": str(amount),
        "display": display_amount(amount, currency_code),
        "plain": format_amount(amount, currency_code),
        "currency": currency_code,
    }


class CreateTransactionSerializer(serializers.Serializer):
    quote_reference = serializers.CharField()
    collect_method = serializers.CharField()
    recipient_name = serializers.CharField(required=False, allow_blank=True, max_length=160)
    recipient_number = serializers.CharField(required=False, allow_blank=True, max_length=32)
    recipient_details = serializers.DictField(required=False)

    def validate_quote_reference(self, value):
        quote = Quote.objects.select_related(
            "corridor__source__currency", "corridor__target__currency"
        ).filter(reference=value).first()
        if quote is None:
            raise serializers.ValidationError("That quote no longer exists. Ask for a new one.")
        return quote

    def validate_collect_method(self, value):
        method = PaymentMethod.objects.filter(slug=value, is_enabled=True).first()
        if method is None:
            raise serializers.ValidationError("That payment method is not available.")
        return method

    def validate(self, attrs):
        quote = attrs["quote_reference"]
        # A send order pays out to a named person in Cameroon; without their
        # details the desk has nowhere to send the money.
        if quote.direction == "send" and not attrs.get("recipient_name"):
            raise serializers.ValidationError(
                {"recipient_name": "Enter the name of the person receiving the money."}
            )
        if quote.direction == "send" and not attrs.get("recipient_number"):
            raise serializers.ValidationError(
                {"recipient_number": "Enter the recipient's Mobile Money number."}
            )
        return attrs


class AttachmentSerializer(serializers.ModelSerializer):
    size_label = serializers.CharField(read_only=True)
    kind = serializers.CharField(read_only=True)
    url = serializers.SerializerMethodField()
    is_purged = serializers.BooleanField(read_only=True)

    class Meta:
        model = Attachment
        fields = ["id", "original_name", "content_type", "size_bytes", "size_label",
                  "kind", "is_payment_proof", "created_at", "url", "is_purged"]

    def get_url(self, obj):
        """A one-minute link, minted per request. Never stored, never cached."""
        from .uploads import signed_url_for

        user = self.context.get("request").user if self.context.get("request") else None
        if user is None:
            return None
        if obj.is_purged:
            # Deleted under the retention policy. The row is still here; the
            # file is not, and a link to it would only 404.
            return None
        try:
            return signed_url_for(obj, user)
        except Exception:  # noqa: BLE001
            return None


class MessageSerializer(serializers.ModelSerializer):
    attachments = AttachmentSerializer(many=True, read_only=True)
    sender_name = serializers.SerializerMethodField()

    class Meta:
        model = Message
        fields = ["id", "kind", "body", "payload", "is_from_desk", "sender_name",
                  "attachments", "read_at", "created_at"]

    def get_sender_name(self, obj):
        if obj.is_from_desk:
            return "NkenzaPay desk"
        return obj.sender.display_name if obj.sender else "Customer"


class StatusHistorySerializer(serializers.ModelSerializer):
    to_label = serializers.SerializerMethodField()
    actor_name = serializers.SerializerMethodField()

    class Meta:
        model = StatusHistory
        fields = ["id", "from_status", "to_status", "to_label", "actor_name",
                  "is_system", "note", "at"]

    def get_to_label(self, obj):
        try:
            return Status(obj.to_status).label
        except ValueError:
            return obj.to_status

    def get_actor_name(self, obj):
        if obj.is_system:
            return "System"
        return obj.actor.display_name if obj.actor else "System"


class TransactionListSerializer(serializers.ModelSerializer):
    send = serializers.SerializerMethodField()
    receive = serializers.SerializerMethodField()
    status_label = serializers.CharField(read_only=True)
    short_status_label = serializers.CharField(read_only=True)
    route = serializers.CharField(source="route_label", read_only=True)
    method = serializers.CharField(source="collect_method.label", read_only=True)

    class Meta:
        model = Transaction
        fields = ["reference", "direction", "status", "status_label", "short_status_label",
                  "route", "method", "send", "receive", "created_at"]

    def get_send(self, obj):
        return money(obj.send_amount, obj.send_currency_id)

    def get_receive(self, obj):
        return money(obj.receive_amount, obj.receive_currency_id)


class TransactionDetailSerializer(TransactionListSerializer):
    """Everything the chat screen and the order-details column need."""

    converted = serializers.SerializerMethodField()
    fee = serializers.SerializerMethodField()
    rate = serializers.SerializerMethodField()
    stepper = serializers.SerializerMethodField()
    history = StatusHistorySerializer(many=True, read_only=True)
    chat_is_locked = serializers.BooleanField(read_only=True)
    payment_reference = serializers.CharField(read_only=True)
    recipient = serializers.SerializerMethodField()
    has_proof = serializers.SerializerMethodField()
    receipt_number = serializers.SerializerMethodField()
    customer = serializers.SerializerMethodField()

    class Meta(TransactionListSerializer.Meta):
        fields = TransactionListSerializer.Meta.fields + [
            "converted", "fee", "fee_percent", "rate", "stepper", "history",
            "chat_is_locked", "payment_reference", "recipient", "has_proof",
            "receipt_number", "customer", "needs_manual_review", "rejected_reason",
            "verified_at", "payout_sent_at", "confirmed_at", "closed_at",
        ]

    def get_converted(self, obj):
        return money(obj.converted_amount, obj.receive_currency_id)

    def get_fee(self, obj):
        return money(obj.fee_amount, obj.receive_currency_id)

    def get_rate(self, obj):
        return {
            "value": str(obj.rate_used),
            "display": (
                f"1 {obj.send_currency_id} = {obj.rate_used.normalize():f} "
                f"{obj.receive_currency_id}"
            ),
        }

    def get_stepper(self, obj):
        """Which step is done, which is live, which is still ahead."""
        history = {h.to_status: h.at for h in obj.history.all()}
        current_index = _step_index(obj.status)
        steps = []
        for index, (label, statuses) in enumerate(STEPPER):
            at = next((history[s] for s in statuses if s in history), None)
            if obj.status in {Status.REJECTED, Status.CANCELLED} and index > current_index:
                state = "stopped"
            elif index < current_index or (at and index != current_index):
                state = "done"
            elif index == current_index:
                state = "current"
            else:
                state = "pending"
            steps.append({"label": label, "state": state, "at": at})
        return steps

    def get_recipient(self, obj):
        if not obj.recipient_name:
            return None
        return {"name": obj.recipient_name, "number": obj.recipient_number,
                "details": obj.recipient_details}

    def get_has_proof(self, obj):
        return obj.attachments.filter(is_payment_proof=True).exists()

    def get_receipt_number(self, obj):
        receipt = getattr(obj, "receipt", None)
        return receipt.number if receipt else None

    def get_customer(self, obj):
        return {"name": obj.user.display_name, "initials": obj.user.initials}


def _step_index(status):
    for index, (_label, statuses) in enumerate(STEPPER):
        if status in statuses:
            return index
    if status in {Status.DISPUTED, Status.REFUND_PENDING}:
        return 4
    if status in {Status.REJECTED, Status.CANCELLED}:
        return 3
    return 0


class ReceiptSerializer(serializers.ModelSerializer):
    reference = serializers.CharField(source="transaction.reference", read_only=True)

    class Meta:
        model = Receipt
        fields = ["number", "reference", "snapshot", "generated_at"]
