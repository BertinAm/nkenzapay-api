from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone


class UserManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, email, password=None, **extra):
        if not email:
            raise ValueError("An email address is required.")
        user = self.model(email=self.normalize_email(email), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("email_verified_at", timezone.now())
        return self.create_user(email, password, **extra)


class User(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(unique=True)
    email_verified_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    is_suspended = models.BooleanField(default=False)
    suspended_reason = models.TextField(blank=True)
    suspended_at = models.DateTimeField(null=True, blank=True)
    marketing_opt_in = models.BooleanField(default=False)
    date_joined = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        ordering = ["-date_joined"]

    def __str__(self):
        return self.email

    @property
    def is_desk(self):
        return hasattr(self, "admin_profile")

    @property
    def display_name(self):
        profile = getattr(self, "profile", None)
        if profile and profile.legal_name:
            return profile.legal_name
        return self.email.split("@")[0]

    @property
    def initials(self):
        profile = getattr(self, "profile", None)
        if profile and profile.first_name:
            return (profile.first_name[:1] + profile.last_name[:1]).upper()
        return self.email[:2].upper()


class Profile(models.Model):
    """The legal name goes in exactly as the government ID spells it. The desk
    reads it off a payment screenshot, so a nickname here costs a rejection."""

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    first_name = models.CharField(max_length=80, blank=True)
    middle_name = models.CharField(max_length=80, blank=True)
    last_name = models.CharField(max_length=80, blank=True)
    whatsapp_country_code = models.CharField(max_length=6, blank=True)
    whatsapp_number = models.CharField(max_length=20, blank=True)
    country = models.ForeignKey("geo.Country", null=True, blank=True, on_delete=models.PROTECT)
    photo_key = models.CharField(max_length=255, blank=True)
    photo_taken_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    IDENTITY_FIELDS = (
        "first_name",
        "middle_name",
        "last_name",
        "whatsapp_country_code",
        "whatsapp_number",
    )

    def __str__(self):
        return self.legal_name or self.user.email

    @property
    def legal_name(self):
        parts = [self.first_name, self.middle_name, self.last_name]
        return " ".join(p for p in parts if p)

    @property
    def whatsapp_display(self):
        if not self.whatsapp_number:
            return ""
        return f"{self.whatsapp_country_code} {self.whatsapp_number}".strip()

    @property
    def is_complete(self):
        return bool(self.first_name and self.last_name and self.whatsapp_number)


class ProfileChangeLog(models.Model):
    """Brief section 38: identity changes are logged, not silently applied."""

    profile = models.ForeignKey(Profile, on_delete=models.CASCADE, related_name="changes")
    field = models.CharField(max_length=40)
    old_value = models.TextField(blank=True)
    new_value = models.TextField(blank=True)
    changed_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-changed_at"]


class SocialIdentity(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="social_identities")
    provider = models.CharField(
        max_length=20, choices=[("google", "Google"), ("apple", "Apple")]
    )
    provider_uid = models.CharField(max_length=190)
    email = models.EmailField(blank=True)
    connected_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("provider", "provider_uid")]


class AdminRole(models.TextChoices):
    OWNER = "owner", "Owner"
    PAYMENTS = "payments", "Payments"
    SUPPORT = "support", "Support"
    READ_ONLY = "read_only", "Read only"


class AdminUser(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="admin_profile")
    role = models.CharField(max_length=20, choices=AdminRole.choices)
    totp_confirmed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.email} ({self.role})"

    @property
    def can_move_money(self):
        """Verify, reject, payout. The actions 2FA is mandatory for."""
        return self.role in {AdminRole.OWNER, AdminRole.PAYMENTS}

    @property
    def can_write_settings(self):
        return self.role == AdminRole.OWNER

    @property
    def can_chat(self):
        return self.role in {AdminRole.OWNER, AdminRole.PAYMENTS, AdminRole.SUPPORT}


class LoginActivity(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="login_activity")
    at = models.DateTimeField(auto_now_add=True, db_index=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    device_label = models.CharField(max_length=120, blank=True)
    is_new_device = models.BooleanField(default=False)
    succeeded = models.BooleanField(default=True)

    class Meta:
        ordering = ["-at"]
        verbose_name_plural = "login activity"


class EmailToken(models.Model):
    """Verification and password reset. Single use, short lived, hashed."""

    PURPOSE_VERIFY = "verify_email"
    PURPOSE_RESET = "password_reset"

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="email_tokens")
    purpose = models.CharField(max_length=24)
    token_hash = models.CharField(max_length=64, db_index=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def is_usable(self):
        return self.used_at is None and self.expires_at > timezone.now()
