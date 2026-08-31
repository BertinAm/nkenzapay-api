from rest_framework import serializers

from .models import Corridor, Country, Currency


class CurrencySerializer(serializers.ModelSerializer):
    class Meta:
        model = Currency
        fields = ["code", "name", "symbol", "minor_units"]


class CountrySerializer(serializers.ModelSerializer):
    currency = CurrencySerializer(read_only=True)

    class Meta:
        model = Country
        fields = [
            "iso2", "name", "currency", "dial_code", "flag_emoji",
            "is_enabled", "is_origin", "is_destination", "sort_order",
        ]


class CorridorSerializer(serializers.ModelSerializer):
    source = CountrySerializer(read_only=True)
    target = CountrySerializer(read_only=True)
    send_currency = serializers.CharField(source="send_currency.code", read_only=True)
    receive_currency = serializers.CharField(source="receive_currency.code", read_only=True)

    class Meta:
        model = Corridor
        fields = ["id", "source", "target", "send_currency", "receive_currency", "is_enabled"]
