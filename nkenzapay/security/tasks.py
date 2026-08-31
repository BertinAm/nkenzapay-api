"""Housekeeping.

Both of these run on a schedule. On shared hosting without a worker they run
from cron instead; see DEPLOYMENT.md.
"""
import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)

# Long enough to investigate an incident, short enough that the table does not
# become the largest thing in the database.
EVENT_RETENTION_DAYS = 180
IDEMPOTENCY_RETENTION_HOURS = 48


@shared_task
def prune_security_data():
    from .models import BlockedAddress, IdempotencyKey, SecurityEvent

    now = timezone.now()

    events, _ = SecurityEvent.objects.filter(
        at__lt=now - timedelta(days=EVENT_RETENTION_DAYS)
    ).delete()

    keys, _ = IdempotencyKey.objects.filter(
        created_at__lt=now - timedelta(hours=IDEMPOTENCY_RETENTION_HOURS)
    ).delete()

    blocks, _ = BlockedAddress.objects.filter(
        expires_at__isnull=False, expires_at__lt=now
    ).delete()

    logger.info(
        "Pruned %s security events, %s idempotency keys, %s expired blocks",
        events, keys, blocks,
    )
    return {"events": events, "keys": keys, "blocks": blocks}
