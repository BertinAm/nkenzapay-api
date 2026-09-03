from decimal import Decimal

from django.db import models
from django.utils import timezone


class FeeRule(models.Model):
    """What NkenzaPay charges, resolved most specific first: corridor, then
    country, then the global rule. Nothing about the charge is in the code."""

    corridor = models.ForeignKey("geo.Corridor", null=True, blank=True,
                                 on_delete=models.CASCADE, related_name="fee_rules")
    country = models.ForeignKey("geo.Country", null=True, blank=True,
                                on_delete=models.CASCADE, related_name="fee_rules")
    direction = models.CharField(max_length=10, blank=True, help_text="Blank applies to both")
    percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("6.00"))
    min_fee = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    max_fee = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    fee_currency = models.ForeignKey("geo.Currency", on_delete=models.PROTECT, related_name="+")
    is_active = models.BooleanField(default=True)
    valid_from = models.DateTimeField(default=timezone.now)
    valid_to = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-valid_from"]

    def __str__(self):
        scope = self.corridor or self.country or "global"
        return f"{self.percent}% ({scope})"

    @property
    def specificity(self):
        """Higher wins. Kept as a property rather than a stored column so a
        rule cannot be edited into the wrong precedence by accident."""
        score = 0
        if self.country_id:
            score += 1
        if self.corridor_id:
            score += 2
        if self.direction:
            score += 1
        return score


class TransferLimit(models.Model):
    """Minimums, maximums and the review threshold. Seeded at 5,000 XAF and
    1,000 INR, both editable from the admin without a deploy."""

    corridor = models.ForeignKey("geo.Corridor", on_delete=models.CASCADE, related_name="limits")
    direction = models.CharField(max_length=10)
    currency = models.ForeignKey("geo.Currency", on_delete=models.PROTECT, related_name="+")
    minimum = models.DecimalField(max_digits=18, decimal_places=4)
    maximum = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    daily_maximum = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    monthly_maximum = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    manual_review_above = models.DecimalField(max_digits=18, decimal_places=4,
                                              null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("corridor", "direction")]

    def __str__(self):
        return f"{self.corridor} {self.direction}: min {self.minimum} {self.currency_id}"


class PlatformSetting(models.Model):
    """Company details and switches from brief section 50 that do not belong to
    fees, methods or countries. One row per key, JSON value."""

    key = models.CharField(max_length=60, primary_key=True)
    value = models.JSONField(default=dict)
    updated_by = models.ForeignKey("accounts.User", null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    DEFAULTS = {
        "company": {
            "name": "NkenzaPay",
            "support_email": "support@nkenzapay.com",
            "support_phone": "+91 80 4718 2200",
            "registered_address": "Bengaluru, Karnataka, India",
            "support_hours": "9am to 9pm IST",
        },
        "security": {
            "admin_2fa_required": True,
            "login_alerts": True,
            "session_expiry": True,
            "session_idle_minutes": 30,
            "manual_review_enabled": True,
        },
        "fraud": {
            "duplicate_contact_check": True,
            "new_device_check": True,
            "max_open_transfers": 3,
        },
        # Canned replies for the desk inbox. Data, not constants: the desk
        # rewrites these as it learns which sentences actually stop a customer
        # worrying, and that should not need a deploy.
        "desk": {
            "quick_replies": [
                "Checking now",
                "Payment verified",
                "Payout sent",
                "Please resend the screenshot",
            ],
        },
    }

    def __str__(self):
        return self.key

    @classmethod
    def get(cls, key, default=None):
        row = cls.objects.filter(pk=key).first()
        if row is not None:
            return row.value
        if default is not None:
            return default
        return cls.DEFAULTS.get(key, {})
