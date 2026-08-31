from django.db import models
from django.utils import timezone


class RateProvider(models.Model):
    """Where the rate comes from. Credentials are never stored here — they live
    in the environment, and only the server ever reads them."""

    slug = models.CharField(max_length=30, unique=True)
    label = models.CharField(max_length=60)
    is_active = models.BooleanField(default=False)
    refresh_seconds = models.PositiveIntegerField(default=60)
    hold_seconds = models.PositiveIntegerField(default=60)
    markup_bps = models.PositiveIntegerField(default=25, help_text="Basis points. 25 = 0.25%")
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    def __str__(self):
        return self.label

    @property
    def markup_multiplier(self):
        from decimal import Decimal

        return Decimal(1) - (Decimal(self.markup_bps) / Decimal(10000))

    @property
    def is_healthy(self):
        if not self.last_success_at:
            return False
        age = (timezone.now() - self.last_success_at).total_seconds()
        return age < self.refresh_seconds * 5


class RateSnapshot(models.Model):
    """One reading of one pair at one moment. Snapshots are never updated; a
    refresh writes a new row, so the rate a transaction used stays provable."""

    provider = models.ForeignKey(RateProvider, on_delete=models.PROTECT, related_name="snapshots")
    base = models.ForeignKey("geo.Currency", on_delete=models.PROTECT, related_name="+")
    quote = models.ForeignKey("geo.Currency", on_delete=models.PROTECT, related_name="+")
    raw_rate = models.DecimalField(max_digits=20, decimal_places=10)
    effective_rate = models.DecimalField(max_digits=20, decimal_places=10)
    fetched_at = models.DateTimeField(db_index=True, default=timezone.now)

    class Meta:
        indexes = [models.Index(fields=["base", "quote", "-fetched_at"])]
        ordering = ["-fetched_at"]

    def __str__(self):
        return f"1 {self.base_id} = {self.effective_rate} {self.quote_id}"

    @property
    def age_seconds(self):
        return (timezone.now() - self.fetched_at).total_seconds()


class Quote(models.Model):
    """A held price. Created whenever a customer asks what they would get, and
    referenced by the order that follows. Its figures are the contract."""

    reference = models.CharField(max_length=32, unique=True)
    user = models.ForeignKey("accounts.User", null=True, blank=True,
                             on_delete=models.SET_NULL, related_name="quotes")
    corridor = models.ForeignKey("geo.Corridor", on_delete=models.PROTECT, related_name="quotes")
    direction = models.CharField(max_length=10)
    snapshot = models.ForeignKey(RateSnapshot, on_delete=models.PROTECT, related_name="quotes")
    send_currency = models.ForeignKey("geo.Currency", on_delete=models.PROTECT, related_name="+")
    receive_currency = models.ForeignKey("geo.Currency", on_delete=models.PROTECT, related_name="+")
    send_amount = models.DecimalField(max_digits=18, decimal_places=4)
    converted_amount = models.DecimalField(max_digits=18, decimal_places=4)
    fee_percent = models.DecimalField(max_digits=5, decimal_places=2)
    fee_amount = models.DecimalField(max_digits=18, decimal_places=4)
    receive_amount = models.DecimalField(max_digits=18, decimal_places=4)
    rate_used = models.DecimalField(max_digits=20, decimal_places=10)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.reference

    @property
    def is_expired(self):
        return timezone.now() >= self.expires_at

    @property
    def seconds_remaining(self):
        return max(0, int((self.expires_at - timezone.now()).total_seconds()))
