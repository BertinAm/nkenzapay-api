import hashlib
import secrets
from datetime import timedelta

from django.contrib.auth import authenticate, login, logout
from django.core.cache import cache
from django.db.models import Count, Q, Sum
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from nkenzapay.audit import services as audit
from nkenzapay.common.exceptions import DomainError
from nkenzapay.notifications import services as notifications
from nkenzapay.security import services as security
from nkenzapay.security.idempotency import idempotent
from nkenzapay.security.models import EventKind, Severity

from .models import EmailToken, LoginActivity, Profile, User
from .serializers import (
    LoginActivitySerializer,
    LoginSerializer,
    PasswordChangeSerializer,
    ProfileSerializer,
    RegisterSerializer,
    UserSerializer,
)
from .uploads import commit_profile_photo, profile_photo_upload_url


class RegisterView(APIView):
    """Open an account.

    Idempotent: a customer whose connection drops mid-signup can retry with the
    same key and get the same account rather than a second one, or a confusing
    "email already taken" for an address that is now theirs.
    """

    permission_classes = [AllowAny]
    throttle_scope = "register"

    @idempotent
    def post(self, request):
        form = RegisterSerializer(data=request.data)
        if not form.is_valid():
            # A rejected sign-up is ordinary; the auto-block threshold decides
            # when a stream of them from one address stops being ordinary.
            security.record(
                EventKind.REGISTRATION_ABUSE,
                request=request,
                summary="Sign-up rejected",
                identifier=str(request.data.get("email", ""))[:190],
                detail={"fields": list(form.errors)},
                severity=Severity.INFO,
            )
            form.is_valid(raise_exception=True)

        user = User.objects.create_user(
            email=form.validated_data["email"],
            password=form.validated_data["password"],
            marketing_opt_in=form.validated_data["marketing_opt_in"],
        )
        notifications.seed_preferences(user)
        issue_email_token(user, EmailToken.PURPOSE_VERIFY)
        notifications.notify(user, "account.welcome")

        login(request, user)
        record_login(request, user, succeeded=True)
        return Response(UserSerializer(user).data, status=status.HTTP_201_CREATED)


class LoginView(APIView):
    permission_classes = [AllowAny]
    throttle_scope = "auth"

    def post(self, request):
        form = LoginSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        email = form.validated_data["email"].lower()

        if is_locked_out(email, request):
            security.record(
                EventKind.LOGIN_LOCKED,
                request=request,
                summary="Sign-in refused while locked out",
                identifier=email,
            )
            raise DomainError(
                "locked_out",
                "Too many failed attempts. Try again in 15 minutes.",
            )

        user = authenticate(request, username=email, password=form.validated_data["password"])
        if user is None:
            note_failure(email, request)
            existing = User.objects.filter(email__iexact=email).first()
            if existing:
                record_login(request, existing, succeeded=False)

            security.record(
                EventKind.LOGIN_FAILED,
                request=request,
                summary="Sign-in failed",
                identifier=email,
                # Whether the address exists is recorded for the desk but never
                # returned: the message below is identical either way.
                detail={"account_exists": bool(existing)},
            )
            raise DomainError("bad_credentials", "That email and password do not match.")

        if user.is_suspended:
            security.record(
                EventKind.PERMISSION_DENIED,
                request=request,
                summary="Suspended account attempted to sign in",
                identifier=email,
                user=user,
            )

        clear_failures(email, request)
        # Django cycles the session key on login, which is what makes a
        # pre-set session id worthless to an attacker.
        login(request, user)
        activity = record_login(request, user, succeeded=True)
        if activity.is_new_device:
            notifications.notify(
                user, "account.login",
                context={"device": activity.device_label or "A new device"},
            )
        return Response(UserSerializer(user).data)


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        logout(request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class SessionView(APIView):
    """Who is signed in. Returns 200 with user null when nobody is, so the
    front end does not have to treat an anonymous visitor as an error."""

    permission_classes = [AllowAny]

    def get(self, request):
        if not request.user.is_authenticated:
            return Response({"user": None})
        return Response({"user": UserSerializer(request.user).data})


class MeView(generics.RetrieveUpdateAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = UserSerializer

    def get_object(self):
        return self.request.user


class MeProfileView(generics.RetrieveUpdateAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = ProfileSerializer

    def get_object(self):
        profile, _ = Profile.objects.get_or_create(user=self.request.user)
        return profile


class ProfilePhotoUploadUrlView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_scope = "upload"

    def post(self, request):
        content_type = request.data.get("content_type", "image/jpeg")
        size = int(request.data.get("size_bytes") or 0)
        return Response(profile_photo_upload_url(request.user, content_type, size))


class ProfilePhotoCommitView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        key = request.data.get("key", "")
        profile = commit_profile_photo(request.user, key)
        return Response(ProfileSerializer(profile, context={"request": request}).data)


class PasswordChangeView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_scope = "auth"

    def post(self, request):
        form = PasswordChangeSerializer(data=request.data, context={"request": request})
        form.is_valid(raise_exception=True)
        if not request.user.check_password(form.validated_data["current_password"]):
            raise DomainError("bad_password", "Your current password is not right.")
        request.user.set_password(form.validated_data["new_password"])
        request.user.save(update_fields=["password"])
        audit.record(actor=request.user, action="account.password_changed",
                     summary=f"{request.user.email} changed their password",
                     target=request.user, request=request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class PasswordResetRequestView(APIView):
    permission_classes = [AllowAny]
    throttle_scope = "password_reset"

    def post(self, request):
        email = (request.data.get("email") or "").strip().lower()
        user = User.objects.filter(email__iexact=email).first()

        security.record(
            EventKind.PASSWORD_RESET_ABUSE,
            request=request,
            summary="Password reset requested",
            identifier=email,
            detail={"account_exists": bool(user)},
            severity=Severity.INFO,
        )

        if user is not None:
            issue_email_token(user, EmailToken.PURPOSE_RESET)
            notifications.notify(user, "account.password_reset")
        # Always the same answer. Confirming which addresses have accounts is a
        # free list for anyone probing.
        return Response({"sent": True})


class PasswordResetConfirmView(APIView):
    permission_classes = [AllowAny]
    throttle_scope = "auth"

    def post(self, request):
        token = request.data.get("token", "")
        new_password = request.data.get("new_password", "")
        record = consume_email_token(token, EmailToken.PURPOSE_RESET)
        if record is None:
            raise DomainError("bad_token", "That reset link has expired. Ask for a new one.")
        from django.contrib.auth import password_validation

        password_validation.validate_password(new_password, record.user)
        record.user.set_password(new_password)
        record.user.save(update_fields=["password"])
        return Response({"reset": True})


class VerifyEmailView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        record = consume_email_token(request.data.get("token", ""),
                                     EmailToken.PURPOSE_VERIFY)
        if record is None:
            raise DomainError("bad_token", "That link has expired. Ask for a new one.")
        record.user.email_verified_at = timezone.now()
        record.user.save(update_fields=["email_verified_at"])
        return Response({"verified": True})


class LoginActivityView(generics.ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = LoginActivitySerializer

    def get_queryset(self):
        return LoginActivity.objects.filter(user=self.request.user)[:50]


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_stats(request):
    """The four dashboard counters."""
    from nkenzapay.transactions.models import Status, Transaction

    rows = Transaction.objects.filter(user=request.user)
    completed = rows.filter(status=Status.COMPLETED)

    received = completed.filter(direction="receive").aggregate(
        total=Sum("receive_amount"), count=Count("id")
    )
    sent = completed.filter(direction="send").aggregate(
        total=Sum("send_amount"), count=Count("id")
    )
    fees = completed.aggregate(total=Sum("fee_amount"))

    return Response({
        "transfers_all_time": rows.count(),
        "in_progress": rows.open().count(),
        "received_total": str(received["total"] or 0),
        "received_count": received["count"],
        "sent_total": str(sent["total"] or 0),
        "sent_count": sent["count"],
        "fees_total": str(fees["total"] or 0),
        "by_status": list(
            rows.values("status").annotate(count=Count("id")).order_by("-count")
        ),
    })


# --- helpers -------------------------------------------------------------


def issue_email_token(user, purpose, ttl_hours=24):
    """Return the raw token. Only its hash is stored, so a database read does
    not hand someone a working reset link."""
    raw = secrets.token_urlsafe(32)
    EmailToken.objects.create(
        user=user,
        purpose=purpose,
        token_hash=hashlib.sha256(raw.encode()).hexdigest(),
        expires_at=timezone.now() + timedelta(hours=ttl_hours),
    )
    return raw


def consume_email_token(raw, purpose):
    if not raw:
        return None
    digest = hashlib.sha256(raw.encode()).hexdigest()
    record = EmailToken.objects.filter(
        token_hash=digest, purpose=purpose, used_at__isnull=True
    ).select_related("user").first()
    if record is None or not record.is_usable:
        return None
    record.used_at = timezone.now()
    record.save(update_fields=["used_at"])
    return record


def record_login(request, user, succeeded):
    agent = request.META.get("HTTP_USER_AGENT", "")[:400]
    ip = audit.client_ip(request)
    label = device_label(agent)
    seen_before = LoginActivity.objects.filter(
        user=user, device_label=label, succeeded=True
    ).exists()
    activity = LoginActivity.objects.create(
        user=user, ip=ip, user_agent=agent, device_label=label,
        is_new_device=not seen_before, succeeded=succeeded,
    )
    if succeeded:
        User.objects.filter(pk=user.pk).update(last_seen_at=timezone.now())
    return activity


def device_label(agent):
    agent_lower = agent.lower()
    if "iphone" in agent_lower:
        platform = "iPhone"
    elif "ipad" in agent_lower:
        platform = "iPad"
    elif "android" in agent_lower:
        platform = "Android"
    elif "mac os" in agent_lower or "macintosh" in agent_lower:
        platform = "Mac"
    elif "windows" in agent_lower:
        platform = "Windows"
    elif "linux" in agent_lower:
        platform = "Linux"
    else:
        platform = "Unknown device"

    for name in ("Edg", "Chrome", "Firefox", "Safari"):
        if name.lower() in agent_lower:
            browser = "Edge" if name == "Edg" else name
            return f"{browser} on {platform}"
    return platform


def _failure_keys(email, request):
    return (
        f"login-fail:email:{email}",
        f"login-fail:ip:{audit.client_ip(request)}",
    )


def is_locked_out(email, request):
    """Lock out after repeated failures.

    Fails open if the cache is unavailable. That is the right trade: a broken
    cache locking every customer out of their own money is a worse outcome than
    briefly losing the lockout, and the failed attempts are still recorded and
    still trigger the address block.
    """
    from django.conf import settings

    try:
        return any(
            (cache.get(key) or 0) >= settings.LOGIN_FAILURE_LIMIT
            for key in _failure_keys(email, request)
        )
    except Exception:  # noqa: BLE001
        return False


def note_failure(email, request):
    from django.conf import settings

    try:
        for key in _failure_keys(email, request):
            count = (cache.get(key) or 0) + 1
            cache.set(key, count, settings.LOGIN_FAILURE_WINDOW_SECONDS)
    except Exception:  # noqa: BLE001
        pass


def clear_failures(email, request):
    try:
        for key in _failure_keys(email, request):
            cache.delete(key)
    except Exception:  # noqa: BLE001
        pass
