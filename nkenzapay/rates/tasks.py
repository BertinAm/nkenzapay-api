import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task
def refresh_rates():
    """Warms every open corridor. Scheduled at the provider's refresh interval."""
    from .services import refresh_all_enabled_corridors

    snapshots = refresh_all_enabled_corridors()
    logger.info("Refreshed %s rate pairs.", len(snapshots))
    return len(snapshots)
