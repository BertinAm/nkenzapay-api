"""Offer the free provider on deployments that already exist.

A migration rather than a line in the seed, because the seed is not safe to
re-run on a live site for this: which provider is live is a commercial decision
somebody made on the desk, and running the seed to pick up a new row would have
been a way to lose it.

Creates the row switched off. Turning it on is a decision, made in Rates and
fees, by someone who means it.
"""
from django.db import migrations


def add_provider(apps, schema_editor):
    RateProvider = apps.get_model("rates", "RateProvider")
    RateProvider.objects.get_or_create(
        slug="exchangerate_api",
        defaults={
            "label": "ExchangeRate-API (free)",
            "is_active": False,
            # The source publishes once a day. Asking every minute would be
            # 1,440 requests to be told the same number 1,439 times.
            "refresh_seconds": 3600,
            "hold_seconds": 60,
            "markup_bps": 25,
        },
    )


def remove_provider(apps, schema_editor):
    RateProvider = apps.get_model("rates", "RateProvider")
    # Never remove one that is live: that would leave the platform with no
    # provider at all and refuse every quote.
    RateProvider.objects.filter(slug="exchangerate_api", is_active=False).delete()


class Migration(migrations.Migration):

    dependencies = [("rates", "0001_initial")]

    operations = [migrations.RunPython(add_provider, remove_provider)]
