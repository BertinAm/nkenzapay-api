from django.db import models

# Every corridor runs between India and somewhere else. India is the fixed end
# of the business: money comes to it from an African country, or goes from it to
# one, and the two customer screens are those two directions. Nothing trades
# Cameroon to Nigeria, and a corridor that did would appear on neither screen.
#
# Named here rather than written into each caller so that opening a second hub
# is one edit and an obvious one, instead of a hunt through the admin API.
HUB_COUNTRY = "IN"


class Currency(models.Model):
    code = models.CharField(max_length=3, primary_key=True)
    name = models.CharField(max_length=60)
    symbol = models.CharField(max_length=6)
    # XAF has no subunit. Nothing here may assume two decimal places.
    minor_units = models.PositiveSmallIntegerField(default=2)

    class Meta:
        verbose_name_plural = "currencies"
        ordering = ["code"]

    def __str__(self):
        return self.code


class Country(models.Model):
    iso2 = models.CharField(max_length=2, primary_key=True)
    name = models.CharField(max_length=80)
    # Optional, because most rows here exist only so somebody can say where
    # they live. A country the platform trades with carries a currency; the
    # other two hundred are places customers happen to be, and inventing a
    # currency for each would be inventing a rate for each.
    currency = models.ForeignKey(Currency, null=True, blank=True,
                                 on_delete=models.PROTECT, related_name="countries")
    dial_code = models.CharField(max_length=6)
    flag_emoji = models.CharField(max_length=8, blank=True)
    is_enabled = models.BooleanField(default=False)
    is_origin = models.BooleanField(default=False)
    is_destination = models.BooleanField(default=False)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        verbose_name_plural = "countries"
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name


class Corridor(models.Model):
    """One direction of travel. Cameroon to India and India to Cameroon are
    two rows, because they carry different fees, limits and methods."""

    source = models.ForeignKey(Country, on_delete=models.PROTECT, related_name="corridors_out")
    target = models.ForeignKey(Country, on_delete=models.PROTECT, related_name="corridors_in")
    is_enabled = models.BooleanField(default=False)

    class Meta:
        unique_together = [("source", "target")]
        ordering = ["source__sort_order", "target__sort_order"]

    def __str__(self):
        return f"{self.source.iso2}->{self.target.iso2}"

    @property
    def send_currency(self):
        return self.source.currency

    @property
    def receive_currency(self):
        return self.target.currency
