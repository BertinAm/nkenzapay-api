from django.db import models


class Dispute(models.Model):
    """A case raised by a customer. The reasons come straight from brief
    section 48 and are shown to the customer in those words."""

    REASONS = [
        ("not_verified", "I made payment but it was not verified"),
        ("not_received", "I have not received my money"),
        ("wrong_amount", "Wrong amount received"),
        ("wrong_details", "Wrong payment details"),
        ("technical", "Technical problem"),
        ("other", "Something else"),
    ]

    RESOLUTIONS = [
        ("closed", "Payout confirmed, case closed"),
        ("resent", "Payout sent again"),
        ("refunded", "Customer refunded"),
        ("escalated", "Escalated"),
    ]

    OPEN = "open"
    RESOLVED = "resolved"
    ESCALATED = "escalated"

    transaction = models.ForeignKey("transactions.Transaction", on_delete=models.CASCADE,
                                    related_name="disputes")
    raised_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    reason_code = models.CharField(max_length=40, choices=REASONS)
    detail = models.TextField(blank=True)
    state = models.CharField(max_length=20, default=OPEN)
    resolution = models.CharField(max_length=40, blank=True, choices=RESOLUTIONS)
    resolution_note = models.TextField(blank=True)
    resolved_by = models.ForeignKey("accounts.User", null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="+")
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.transaction.reference}: {self.reason_code}"

    @classmethod
    def reason_label(cls, code):
        return dict(cls.REASONS).get(code, "Problem reported")

    @property
    def reason_display(self):
        return self.reason_label(self.reason_code)
