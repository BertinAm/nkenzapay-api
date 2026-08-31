from django.db import models


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
    currency = models.ForeignKey(Currency, on_delete=models.PROTECT, related_name="countries")
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
