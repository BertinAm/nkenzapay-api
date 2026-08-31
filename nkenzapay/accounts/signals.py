from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Profile, User


@receiver(post_save, sender=User)
def ensure_profile(sender, instance, created, **kwargs):
    """Every user has a profile row from the moment the account exists, so the
    onboarding screens can PATCH it rather than deciding whether to create it."""
    if created:
        Profile.objects.get_or_create(user=instance)
