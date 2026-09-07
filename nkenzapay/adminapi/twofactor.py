"""Two-factor enrolment for desk accounts.

django_otp and its TOTP plugin have been installed since the beginning, the
middleware has been running, and nothing ever used them. AdminUser carries a
`totp_confirmed_at` that every money-moving path checks and that no code could
set, so a freshly seeded owner could sign in, read everything, and verify
nothing.

The QR is drawn here rather than by the browser or a QR service. The payload is
a shared secret; handing it to a third party to render would give that party
everything it needs to produce codes.
"""
import io

import qrcode
import qrcode.image.svg
from django.utils import timezone
from django_otp.plugins.otp_totp.models import TOTPDevice
from rest_framework.response import Response
from rest_framework.views import APIView

from nkenzapay.audit import services as audit
from nkenzapay.common.exceptions import DomainError

from .permissions import IsDesk

DEVICE_NAME = "desk"


class TwoFactorStatus(APIView):
    """Whether this account has finished enrolling."""

    permission_classes = [IsDesk]

    def get(self, request):
        profile = request.user.admin_profile
        return Response({
            "enrolled": profile.totp_confirmed_at is not None,
            "confirmed_at": profile.totp_confirmed_at,
            "role": profile.role,
        })


class TwoFactorSetup(APIView):
    """Start enrolment: a fresh secret, its QR, and the code to type by hand.

    Refused once enrolled. Someone who has taken a session should not be able
    to quietly re-bind two-factor to a device of their own and lock the real
    owner out; that needs a person, not an endpoint.
    """

    permission_classes = [IsDesk]

    def post(self, request):
        profile = request.user.admin_profile
        if profile.totp_confirmed_at is not None:
            raise DomainError(
                "already_enrolled",
                "This account already has two-factor set up. To move it to a "
                "new phone, ask an owner to reset it first.",
            )

        # Only ever one half-finished enrolment per account.
        TOTPDevice.objects.filter(user=request.user, confirmed=False).delete()
        device = TOTPDevice.objects.create(
            user=request.user, name=DEVICE_NAME, confirmed=False
        )

        return Response({
            "otpauth_url": device.config_url,
            # Grouped for reading aloud or typing without losing your place.
            "secret": _grouped(device.bin_key),
            "qr_svg": _qr_svg(device.config_url),
        })


class TwoFactorConfirm(APIView):
    """Finish enrolment by proving the phone produces the right code."""

    permission_classes = [IsDesk]

    def post(self, request):
        profile = request.user.admin_profile
        if profile.totp_confirmed_at is not None:
            raise DomainError("already_enrolled",
                              "This account already has two-factor set up.")

        device = TOTPDevice.objects.filter(
            user=request.user, confirmed=False
        ).order_by("-id").first()
        if device is None:
            raise DomainError("no_setup",
                              "Start again — there is no enrolment in progress.")

        code = (request.data.get("code") or "").replace(" ", "")
        if not device.verify_token(code):
            # verify_token also refuses a code already used, so a shoulder-surfed
            # code cannot be replayed within its window.
            raise DomainError(
                "bad_code",
                "That code was not right. Check the clock on your phone is "
                "accurate, then try the next one.",
            )

        device.confirmed = True
        device.save(update_fields=["confirmed"])
        profile.totp_confirmed_at = timezone.now()
        profile.save(update_fields=["totp_confirmed_at"])

        audit.record(
            actor=request.user, action="admin.2fa_enrolled",
            summary=f"{request.user.email} set up two-factor authentication",
            target=profile, request=request,
        )
        return Response({"enrolled": True, "confirmed_at": profile.totp_confirmed_at})


def _grouped(key: bytes) -> str:
    import base64

    secret = base64.b32encode(key).decode().rstrip("=")
    return " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))


def _qr_svg(config_url: str) -> str:
    """An SVG rather than a PNG: it scales to whatever the screen is, and it
    goes into the page as markup instead of a second request."""
    image = qrcode.make(
        config_url,
        image_factory=qrcode.image.svg.SvgPathImage,
        box_size=10,
        border=2,
    )
    buffer = io.BytesIO()
    image.save(buffer)
    return buffer.getvalue().decode()
