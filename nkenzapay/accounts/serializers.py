import re

from django.contrib.auth import password_validation
from django.utils import timezone
from rest_framework import serializers

from nkenzapay.geo.models import Country

from .models import LoginActivity, Profile, User

# Accepts the shapes a customer actually types: +91 00000 00000, 0600000000,
# 600 000 000. Stored digits-only with the code kept separately.
PHONE_CLEAN = re.compile(r"[^\d]")


class RegisterSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, min_length=10)
    marketing_opt_in = serializers.BooleanField(default=False)

    def validate_email(self, value):
        normalised = value.strip().lower()
        if User.objects.filter(email__iexact=normalised).exists():
            raise serializers.ValidationError(
                "An account already uses this email address. Log in instead."
            )
        return normalised

    def validate_password(self, value):
        password_validation.validate_password(value)
        return value


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)


class ProfileSerializer(serializers.ModelSerializer):
    legal_name = serializers.CharField(read_only=True)
    whatsapp_display = serializers.CharField(read_only=True)
    is_complete = serializers.BooleanField(read_only=True)
    country = serializers.PrimaryKeyRelatedField(
        queryset=Country.objects.all(), allow_null=True, required=False
    )
    has_photo = serializers.SerializerMethodField()

    class Meta:
        model = Profile
        fields = [
            "first_name", "middle_name", "last_name", "legal_name",
            "whatsapp_country_code", "whatsapp_number", "whatsapp_display",
            "country", "has_photo", "photo_taken_at", "is_complete", "completed_at",
        ]
        read_only_fields = ["photo_taken_at", "completed_at"]

    def get_has_photo(self, obj):
        return bool(obj.photo_key)

    def validate_whatsapp_number(self, value):
        digits = PHONE_CLEAN.sub("", value or "")
        if value and len(digits) < 6:
            raise serializers.ValidationError("That does not look like a phone number.")
        return digits

    def validate_whatsapp_country_code(self, value):
        if not value:
            return value
        code = value.strip()
        if not code.startswith("+"):
            code = "+" + code.lstrip("+")
        if not code[1:].isdigit():
            raise serializers.ValidationError("Country code should be digits, like +91.")
        return code

    def update(self, instance, validated_data):
        """Identity changes are logged before they are applied (brief 38)."""
        from .models import ProfileChangeLog

        actor = self.context["request"].user
        for field in Profile.IDENTITY_FIELDS:
            if field not in validated_data:
                continue
            old = getattr(instance, field) or ""
            new = validated_data[field] or ""
            if str(old) != str(new):
                ProfileChangeLog.objects.create(
                    profile=instance, field=field, old_value=str(old),
                    new_value=str(new), changed_by=actor,
                )

        profile = super().update(instance, validated_data)
        if profile.is_complete and profile.completed_at is None:
            profile.completed_at = timezone.now()
            profile.save(update_fields=["completed_at"])
        return profile


class UserSerializer(serializers.ModelSerializer):
    profile = ProfileSerializer(read_only=True)
    display_name = serializers.CharField(read_only=True)
    initials = serializers.CharField(read_only=True)
    is_desk = serializers.BooleanField(read_only=True)
    admin_role = serializers.SerializerMethodField()
    needs_onboarding = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id", "email", "email_verified_at", "marketing_opt_in", "date_joined",
            "is_suspended", "profile", "display_name", "initials", "is_desk",
            "admin_role", "needs_onboarding",
        ]
        read_only_fields = ["id", "email", "email_verified_at", "date_joined", "is_suspended"]

    def get_admin_role(self, obj):
        admin_profile = getattr(obj, "admin_profile", None)
        return admin_profile.role if admin_profile else None

    def get_needs_onboarding(self, obj):
        profile = getattr(obj, "profile", None)
        return not (profile and profile.is_complete)


class LoginActivitySerializer(serializers.ModelSerializer):
    class Meta:
        model = LoginActivity
        fields = ["id", "at", "ip", "device_label", "is_new_device", "succeeded"]


class PasswordChangeSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True, min_length=10)

    def validate_new_password(self, value):
        password_validation.validate_password(value, self.context["request"].user)
        return value
