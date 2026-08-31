from django.db import models
from django.utils import timezone


class Status(models.TextChoices):
    ORDER_CREATED = "order_created", "Order created"
    AWAITING_PAYMENT = "awaiting_payment", "Awaiting payment"
    PROOF_SUBMITTED = "proof_submitted", "Payment proof submitted"
    PAYMENT_VERIFICATION = "payment_verification", "Payment verification"
    PAYMENT_CONFIRMED = "payment_confirmed", "Payment confirmed"
    PAYOUT_PROCESSING = "payout_processing", "Payout processing"
    PAYOUT_SENT = "payout_sent", "Payout sent"
    AWAITING_CONFIRMATION = "awaiting_confirmation", "Awaiting customer confirmation"
    COMPLETED = "completed", "Completed"
    CANCELLED = "cancelled", "Cancelled"
    REJECTED = "rejected", "Rejected"
    DISPUTED = "disputed", "Disputed"
    REFUND_PENDING = "refund_pending", "Refund / correction pending"


# Statuses that close the transfer. The chat goes read-only, the receipt is
# fixed, and nothing further may be posted by either side.
CLOSED_STATUSES = {Status.COMPLETED, Status.CANCELLED, Status.REJECTED}

# The short words the admin queue uses, where a column is too narrow for the
# full status. Same colour mapping, shorter label.
SHORT_LABELS = {
    Status.ORDER_CREATED: "Created",
    Status.AWAITING_PAYMENT: "Awaiting",
    Status.PROOF_SUBMITTED: "Verify",
    Status.PAYMENT_VERIFICATION: "Verify",
    Status.PAYMENT_CONFIRMED: "Payout",
    Status.PAYOUT_PROCESSING: "Payout",
    Status.PAYOUT_SENT: "Confirm",
    Status.AWAITING_CONFIRMATION: "Confirm",
    Status.COMPLETED: "Done",
    Status.CANCELLED: "Cancelled",
    Status.REJECTED: "Rejected",
    Status.DISPUTED: "Dispute",
    Status.REFUND_PENDING: "Refund",
}


class TransactionQuerySet(models.QuerySet):
    def needs_desk(self):
        return self.filter(status__in=[Status.PROOF_SUBMITTED, Status.PAYMENT_VERIFICATION])

    def open(self):
        return self.exclude(status__in=CLOSED_STATUSES)

    def for_user(self, user):
        return self.filter(user=user)


class Transaction(models.Model):
    """One order. Its commercial terms are frozen at creation and never
    recalculated — not on a rate refresh, not on a fee change, not ever."""

    reference = models.CharField(max_length=24, unique=True)
    user = models.ForeignKey("accounts.User", on_delete=models.PROTECT,
                             related_name="transactions")
    corridor = models.ForeignKey("geo.Corridor", on_delete=models.PROTECT, related_name="+")
    direction = models.CharField(max_length=10)
    status = models.CharField(max_length=30, choices=Status.choices,
                              default=Status.ORDER_CREATED, db_index=True)

    quote = models.ForeignKey("rates.Quote", on_delete=models.PROTECT, related_name="+")
    rate_used = models.DecimalField(max_digits=20, decimal_places=10)
    fee_percent = models.DecimalField(max_digits=5, decimal_places=2)
    send_currency = models.ForeignKey("geo.Currency", on_delete=models.PROTECT, related_name="+")
    receive_currency = models.ForeignKey("geo.Currency", on_delete=models.PROTECT,
                                         related_name="+")
    send_amount = models.DecimalField(max_digits=18, decimal_places=4)
    converted_amount = models.DecimalField(max_digits=18, decimal_places=4)
    fee_amount = models.DecimalField(max_digits=18, decimal_places=4)
    receive_amount = models.DecimalField(max_digits=18, decimal_places=4)

    collect_method = models.ForeignKey("payments.PaymentMethod", on_delete=models.PROTECT,
                                       related_name="+")
    payout_method = models.ForeignKey("payments.PaymentMethod", null=True, blank=True,
                                      on_delete=models.PROTECT, related_name="+")
    recipient_name = models.CharField(max_length=160, blank=True)
    recipient_number = models.CharField(max_length=32, blank=True)
    recipient_details = models.JSONField(default=dict, blank=True)

    needs_manual_review = models.BooleanField(default=False)
    verified_by = models.ForeignKey("accounts.User", null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="+")
    verified_at = models.DateTimeField(null=True, blank=True)
    rejected_reason = models.TextField(blank=True)
    payout_sent_at = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = TransactionQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["status", "-created_at"]),
        ]

    def __str__(self):
        return self.reference

    @property
    def chat_is_locked(self):
        return self.status in CLOSED_STATUSES

    @property
    def status_label(self):
        return Status(self.status).label

    @property
    def short_status_label(self):
        return SHORT_LABELS.get(self.status, self.status_label)

    @property
    def route_label(self):
        return f"{self.corridor.source.name} to {self.corridor.target.name}"

    @property
    def instruction(self):
        return getattr(self.collect_method, "instruction", None)

    @property
    def payment_reference(self):
        instruction = self.instruction
        return instruction.reference(self) if instruction else self.reference


class StatusHistory(models.Model):
    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name="history")
    from_status = models.CharField(max_length=30, blank=True)
    to_status = models.CharField(max_length=30)
    actor = models.ForeignKey("accounts.User", null=True, blank=True,
                              on_delete=models.SET_NULL, related_name="+")
    is_system = models.BooleanField(default=False)
    note = models.TextField(blank=True)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["at"]
        verbose_name_plural = "status history"


class MessageKind(models.TextChoices):
    TEXT = "text", "Text"
    SYSTEM_NOTICE = "system_notice", "System notice"
    SYSTEM_INSTRUCTIONS = "system_instructions", "Payment instructions"
    ATTACHMENT = "attachment", "Attachment"
    ACTION = "action", "Action"


class Message(models.Model):
    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE,
                                    related_name="messages")
    sender = models.ForeignKey("accounts.User", null=True, blank=True,
                               on_delete=models.SET_NULL, related_name="+")
    is_from_desk = models.BooleanField(default=False)
    kind = models.CharField(max_length=24, choices=MessageKind.choices,
                            default=MessageKind.TEXT)
    body = models.TextField(blank=True)
    payload = models.JSONField(default=dict, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.transaction_id}: {self.body[:40]}"


class Attachment(models.Model):
    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE,
                                    related_name="attachments")
    message = models.ForeignKey(Message, null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="attachments")
    uploaded_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    storage_key = models.CharField(max_length=255)
    original_name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100)
    size_bytes = models.PositiveIntegerField()
    checksum = models.CharField(max_length=64, blank=True)
    is_payment_proof = models.BooleanField(default=False)
    scanned_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    # Set when the file itself has been deleted under the retention policy.
    # The row outlives the file on purpose: the thread should still show that
    # a document was sent, by whom and when, after the document is gone.
    purged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return self.original_name

    @property
    def is_purged(self):
        return bool(self.purged_at) or not self.storage_key

    @property
    def kind(self):
        if self.content_type.startswith("image/"):
            return "image"
        if self.content_type.startswith("video/"):
            return "video"
        return "document"

    @property
    def size_label(self):
        kb = self.size_bytes / 1024
        if kb < 1024:
            return f"{kb:.0f} KB"
        return f"{kb / 1024:.1f} MB"


class Receipt(models.Model):
    """Generated once, on completion. The snapshot holds every rendered field
    so a later fee change cannot rewrite history."""

    transaction = models.OneToOneField(Transaction, on_delete=models.CASCADE,
                                       related_name="receipt")
    number = models.CharField(max_length=24, unique=True)
    pdf_key = models.CharField(max_length=255, blank=True)
    snapshot = models.JSONField(default=dict)
    generated_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.number


class TransactionCounter(models.Model):
    """Per-day sequence behind NKP-YYYYMMDD-NNNNN.

    A row per day, incremented inside the same transaction that creates the
    order, so two customers pressing Create order at once cannot collide.
    """

    day = models.DateField(primary_key=True)
    last_number = models.PositiveIntegerField(default=0)

    @classmethod
    def next_reference(cls, at=None):
        at = at or timezone.now()
        day = at.date()
        counter, _ = cls.objects.select_for_update().get_or_create(day=day)
        counter.last_number += 1
        counter.save(update_fields=["last_number"])
        return f"NKP-{day:%Y%m%d}-{counter.last_number:05d}"
