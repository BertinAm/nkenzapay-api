"""Orange Money as a payout network, not only a collection one.

Cameroon had MTN and Orange on the collect side and only MTN on the payout
side, which was invisible while nothing asked. It stops being invisible now
that the customer says which network the recipient's number is on: without this
row, every Orange number in Cameroon had nowhere to go.

A migration rather than a line in the seed alone, because the seed is not the
way changes reach a deployment that is already running.
"""
from django.db import migrations


def add_orange_payout(apps, schema_editor):
    Country = apps.get_model("geo", "Country")
    PaymentMethod = apps.get_model("payments", "PaymentMethod")

    if not Country.objects.filter(pk="CM").exists():
        return

    PaymentMethod.objects.get_or_create(
        slug="orange_payout",
        defaults={
            "label": "Orange Money",
            "country_id": "CM",
            "side": "payout",
            "icon": "smartphone",
            "note": "Instant",
            "is_enabled": True,
            "sort_order": 2,
        },
    )


def remove_orange_payout(apps, schema_editor):
    PaymentMethod = apps.get_model("payments", "PaymentMethod")
    PaymentMethod.objects.filter(slug="orange_payout").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("payments", "0001_initial"),
        ("geo", "0001_initial"),
    ]

    operations = [migrations.RunPython(add_orange_payout, remove_orange_payout)]
