from django.db import models


class PaymentMethod(models.Model):
    """A way money moves in or out. Turning one off hides it from the quote
    page on the next request — no deploy, no code change."""

    COLLECT = "collect"
    PAYOUT = "payout"
    SIDES = [(COLLECT, "Collect"), (PAYOUT, "Payout")]

    slug = models.CharField(max_length=40, unique=True)
    label = models.CharField(max_length=60)
    country = models.ForeignKey("geo.Country", on_delete=models.CASCADE,
                                related_name="payment_methods")
    side = models.CharField(max_length=10, choices=SIDES)
    icon = models.CharField(max_length=40, default="payments",
                            help_text="Material Symbols Rounded name")
    note = models.CharField(max_length=40, blank=True, help_text="Instant, NEFT/RTGS, CBDC")
    is_enabled = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["country__sort_order", "sort_order", "label"]

    def __str__(self):
        return f"{self.label} ({self.country_id})"

    @property
    def summary(self):
        """The one-line detail the admin list shows beside the toggle."""
        instruction = getattr(self, "instruction", None)
        if not instruction:
            return ""
        return instruction.summary


class PaymentInstruction(models.Model):
    """What the customer is told to pay into, per method.

    The field set differs by method (brief section 34), so the fields live in
    JSON rather than in twenty nullable columns. The chat template reads from
    here; nothing about a bank account number is ever written in code.
    """

    method = models.OneToOneField(PaymentMethod, on_delete=models.CASCADE,
                                  related_name="instruction")
    fields = models.JSONField(default=dict, blank=True)
    body = models.TextField(blank=True, help_text="Free text, shown under the fields")
    qr_key = models.CharField(max_length=255, blank=True)
    reference_format = models.CharField(max_length=40, default="NKP-{order}")
    updated_by = models.ForeignKey("accounts.User", null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    # Which keys each method expects, and the order they are shown in. Used by
    # the chat instruction block and validated on save in the admin API.
    FIELD_SETS = {
        "mtn_momo": ["number", "account_name"],
        "orange_money": ["number", "account_name"],
        "upi": ["upi_id", "merchant_name"],
        "bank": ["account_holder", "bank", "account_number", "ifsc", "branch"],
        "imps": ["account_holder", "bank", "account_number", "ifsc", "branch"],
        "cbdc": ["wallet_id", "wallet_name"],
        "cash": ["location", "contact"],
    }

    LABELS = {
        "number": "Number",
        "account_name": "Account name",
        "upi_id": "UPI ID",
        "merchant_name": "Merchant name",
        "account_holder": "Account holder",
        "bank": "Bank",
        "account_number": "Account number",
        "ifsc": "IFSC",
        "branch": "Branch",
        "wallet_id": "Wallet ID",
        "wallet_name": "Wallet name",
        "location": "Location",
        "contact": "Contact",
    }

    def __str__(self):
        return f"Instructions for {self.method.label}"

    @property
    def summary(self):
        values = [str(v) for v in self.ordered_fields().values() if v]
        return " · ".join(values[:3])

    def ordered_fields(self):
        keys = self.FIELD_SETS.get(self.method.slug) or list(self.fields.keys())
        return {k: self.fields.get(k, "") for k in keys}

    def masked_fields(self):
        """What an unauthenticated visitor may see on the quote page: enough to
        recognise the account, not enough to be useful to anyone else."""
        masked = {}
        for key, value in self.ordered_fields().items():
            text = str(value or "")
            if key in {"account_name", "merchant_name", "account_holder", "bank",
                       "wallet_name", "location"}:
                masked[key] = text
            elif len(text) > 3:
                masked[key] = text[:2] + " " + "•" * max(3, len(text) - 4) + text[-1:]
            else:
                masked[key] = "•" * len(text)
        return masked

    def rows_for_chat(self, transaction=None):
        """The mono rows in the payment instructions bubble."""
        rows = [
            {"label": self.LABELS.get(key, key.replace("_", " ").title()),
             "value": str(value),
             "copyable": True}
            for key, value in self.ordered_fields().items()
            if value
        ]
        if transaction is not None:
            from nkenzapay.common.money import format_amount

            rows.append({
                "label": "Amount",
                "value": (
                    f"{format_amount(transaction.send_amount, transaction.send_currency_id)} "
                    f"{transaction.send_currency_id}"
                ),
                "copyable": True,
            })
            rows.append({
                "label": "Reference",
                "value": self.reference(transaction),
                "copyable": True,
            })
        return rows

    def reference(self, transaction):
        order_number = transaction.reference.split("-")[-1]
        return self.reference_format.format(
            order=order_number, reference=transaction.reference
        )
