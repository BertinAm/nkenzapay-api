"""Let anybody open an account, wherever they live.

Signing up asked for a country and offered the six the platform trades with, so
a Cameroonian studying in Germany could not say where they were — and that is
exactly the person this service is for.

These rows are residence only: no currency, no corridors, and switched off for
trading in every sense. The countries screen and the public list both filter on
that, so nothing commercial changes and the marketing page still names the
handful that are actually open.

Existing rows are left exactly as they are. Cameroon and India are already here
with their currencies attached, and this must not touch them.
"""
from django.db import migrations

from nkenzapay.geo.iso3166 import COUNTRIES, flag_for


def add_residence_countries(apps, schema_editor):
    Country = apps.get_model("geo", "Country")

    known = set(Country.objects.values_list("pk", flat=True))
    # After the ones the desk has opened, so a sorted list still leads with the
    # countries that mean something here.
    base = 1000

    Country.objects.bulk_create(
        [
            Country(
                iso2=iso2,
                name=name,
                currency=None,
                dial_code=dial,
                flag_emoji=flag_for(iso2),
                is_enabled=False,
                is_origin=False,
                is_destination=False,
                sort_order=base + index,
            )
            for index, (iso2, name, dial) in enumerate(COUNTRIES)
            if iso2 not in known
        ],
        # Nothing here should collide, since the ones already present are
        # skipped, but a half-applied run must be safe to repeat.
        ignore_conflicts=True,
    )


def remove_residence_countries(apps, schema_editor):
    Country = apps.get_model("geo", "Country")
    # Only the ones nobody trades with and nobody has claimed as their own.
    Country.objects.filter(
        currency__isnull=True,
        is_enabled=False,
        is_origin=False,
        is_destination=False,
        profile__isnull=True,
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("geo", "0002_country_currency_optional"),
        ("accounts", "0002_profile_id_document_key_profile_id_document_type_and_more"),
    ]

    operations = [
        migrations.RunPython(add_residence_countries, remove_residence_countries)
    ]
