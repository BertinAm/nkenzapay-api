from django.db import models


class Notification(models.Model):
    """One row per event per recipient. The desk feed and the customer centre
    are the same table filtered by audience."""

    CUSTOMER = "customer"
    ADMIN = "admin"

    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE,
                             related_name="notifications")
    audience = models.CharField(max_length=10, default=CUSTOMER)
    event = models.CharField(max_length=48, db_index=True)
    title = models.CharField(max_length=140)
    body = models.CharField(max_length=280, blank=True)
    icon = models.CharField(max_length=40, default="notifications")
    tone = models.CharField(max_length=12, default="neutral")
    transaction = models.ForeignKey("transactions.Transaction", null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="notifications")
    action = models.CharField(max_length=40, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    emailed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "read_at", "-created_at"])]

    def __str__(self):
        return f"{self.event} -> {self.user_id}"

    @property
    def is_unread(self):
        return self.read_at is None

    @property
    def category(self):
        """Which tab this belongs under.

        Desk events are named admin.<what happened>, and what files them is
        what happened rather than who it reached — so the prefix comes off
        first. Without that every desk notification lands in "system" and the
        desk's tabs all show the same list.
        """
        event = self.event
        if event.startswith("admin."):
            event = event.split(".", 1)[1]

        if event.startswith("message"):
            return "messages"
        if event.startswith(("news", "promo")):
            return "company"
        if event.startswith("dispute"):
            return "disputes"
        if event.startswith(("admin", "system", "rate", "new_device", "login")):
            return "system"
        return "transfers"


class NotificationPreference(models.Model):
    """Transfer updates are locked on. A customer cannot switch off the message
    that tells them their money moved."""

    TRANSFER_UPDATES = "transfer_updates"
    CHAT = "chat"
    RATE_EXPIRY = "rate_expiry"
    MARKETING = "marketing"
    LOGIN = "login"

    GROUPS = [
        (TRANSFER_UPDATES, "Transfer updates", True),
        (CHAT, "Chat messages", False),
        (RATE_EXPIRY, "Rate expiry", False),
        (MARKETING, "News and promotions", False),
        (LOGIN, "Login alerts", False),
    ]

    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE,
                             related_name="notification_preferences")
    channel_group = models.CharField(max_length=40)
    in_app = models.BooleanField(default=True)
    email = models.BooleanField(default=True)
    push = models.BooleanField(default=False)
    is_locked = models.BooleanField(default=False)

    class Meta:
        unique_together = [("user", "channel_group")]

    def __str__(self):
        return f"{self.user_id}: {self.channel_group}"


class DeliveryRule(models.Model):
    """Which desk events also send an email. Editable from admin notifications."""

    event = models.CharField(max_length=48, unique=True)
    label = models.CharField(max_length=120)
    email_admins = models.BooleanField(default=False)
    email_customer = models.BooleanField(default=False)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "label"]

    def __str__(self):
        return self.label
