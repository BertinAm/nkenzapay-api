"""Refresh the FX rate for every open corridor.

Run from cron where there is no Celery worker. Safe to run every minute: the
provider's own refresh interval decides whether a call is actually made.
"""
from django.core.management.base import BaseCommand

from nkenzapay.rates.providers import RateUnavailable
from nkenzapay.rates.services import refresh_all_enabled_corridors


class Command(BaseCommand):
    help = "Fetch current rates for every enabled corridor."

    def handle(self, *args, **options):
        try:
            snapshots = refresh_all_enabled_corridors()
        except RateUnavailable as exc:
            # Exit non-zero so cron mail carries the reason.
            raise SystemExit(f"Rates unavailable: {exc}")

        for snapshot in snapshots:
            self.stdout.write(f"  {snapshot}")
        self.stdout.write(self.style.SUCCESS(f"Refreshed {len(snapshots)} pairs."))
