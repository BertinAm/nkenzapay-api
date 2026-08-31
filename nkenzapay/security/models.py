from django.db import models
from django.utils import timezone


class Severity(models.TextChoices):
    INFO = "info", "Info"
    LOW = "low", "Low"
    MEDIUM = "medium", "Medium"
    HIGH = "high", "High"
    CRITICAL = "critical", "Critical"


class EventKind(models.TextChoices):
    """What the platform saw.

    Deliberately specific. "Suspicious activity" tells the desk nothing; "SQL
    injection probe in the q parameter" tells them what to do next.
    """

    LOGIN_FAILED = "login_failed", "Failed login"
    LOGIN_LOCKED = "login_locked", "Account locked after repeated failures"
    LOGIN_NEW_DEVICE = "login_new_device", "Sign-in from a new device"
    PASSWORD_RESET_ABUSE = "password_reset_abuse", "Repeated password reset requests"
    REGISTRATION_ABUSE = "registration_abuse", "Repeated sign-ups from one address"
    RATE_LIMITED = "rate_limited", "Rate limit reached"
    CSRF_FAILED = "csrf_failed", "CSRF check failed"
    PERMISSION_DENIED = "permission_denied", "Permission denied"
    INJECTION_PROBE = "injection_probe", "Injection probe"
    TRAVERSAL_PROBE = "traversal_probe", "Path traversal probe"
    SCANNER = "scanner", "Automated scanner"
    BAD_UPLOAD = "bad_upload", "Rejected upload"
    IDEMPOTENCY_REPLAY = "idempotency_replay", "Duplicate request replayed"
    QUOTE_ABUSE = "quote_abuse", "Excessive quote requests"
    ENUMERATION = "enumeration", "Account or reference enumeration"
    ADMIN_ACTION_DENIED = "admin_action_denied", "Desk action refused"
    BLOCKED = "blocked", "Request from a blocked address"


class SecurityEvent(models.Model):
    """One thing worth knowing about, recorded as it happened.

    Written from middleware and from the places that already know something is
    wrong (a failed login, a rejected upload). Append-only in practice: nothing
    in the product updates a row after it is written.
    """

    kind = models.CharField(max_length=32, choices=EventKind.choices, db_index=True)
    severity = models.CharField(max_length=10, choices=Severity.choices,
                                default=Severity.LOW, db_index=True)
    summary = models.CharField(max_length=280)

    ip = models.GenericIPAddressField(null=True, blank=True, db_index=True)
    user = models.ForeignKey("accounts.User", null=True, blank=True,
                             on_delete=models.SET_NULL, related_name="security_events")
    # Kept even when there is no account, so a wrong email is still traceable.
    identifier = models.CharField(max_length=190, blank=True, db_index=True)

    method = models.CharField(max_length=8, blank=True)
    path = models.CharField(max_length=300, blank=True)
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    referer = models.CharField(max_length=300, blank=True)
    country = models.CharField(max_length=2, blank=True)

    # What was actually seen. Never the raw body: a probe payload is untrusted
    # input and the desk reads this in a browser.
    detail = models.JSONField(default=dict, blank=True)

    at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-at"]
        indexes = [
            models.Index(fields=["ip", "-at"]),
            models.Index(fields=["kind", "-at"]),
            models.Index(fields=["severity", "-at"]),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} from {self.ip or 'unknown'}"


class BlockedAddress(models.Model):
    """An address the platform is refusing.

    Blocks expire. A permanent block on a shared or mobile-carrier IP quietly
    locks out people who did nothing, and in this corridor a lot of customers
    share one.
    """

    ip = models.GenericIPAddressField(unique=True)
    reason = models.CharField(max_length=280)
    kind = models.CharField(max_length=32, choices=EventKind.choices, blank=True)
    hits = models.PositiveIntegerField(default=1)
    is_automatic = models.BooleanField(default=True)
    blocked_by = models.ForeignKey("accounts.User", null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="+")
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "blocked addresses"

    def __str__(self):
        return self.ip

    @property
    def is_active(self):
        return self.expires_at is None or self.expires_at > timezone.now()

    @property
    def is_permanent(self):
        return self.expires_at is None


class IdempotencyKey(models.Model):
    """One record per client-supplied key, so a retried request does not create
    a second account, a second order or a second payout.

    The stored response is replayed verbatim, which is what makes a retry safe:
    the caller gets the same answer, not a duplicate side effect and not an
    error that makes them try again.
    """

    key = models.CharField(max_length=200)
    user = models.ForeignKey("accounts.User", null=True, blank=True,
                             on_delete=models.CASCADE, related_name="+")
    # Anonymous callers are scoped by address so one visitor's key cannot
    # collide with, or replay, another's.
    scope = models.CharField(max_length=100, default="")
    method = models.CharField(max_length=8)
    path = models.CharField(max_length=300)
    # Hash of the body, so the same key with different content is rejected
    # rather than silently returning the first response.
    request_fingerprint = models.CharField(max_length=64)

    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    response_body = models.JSONField(null=True, blank=True)
    is_complete = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [("key", "scope")]
        indexes = [models.Index(fields=["-created_at"])]

    def __str__(self):
        return f"{self.method} {self.path} [{self.key[:12]}…]"
