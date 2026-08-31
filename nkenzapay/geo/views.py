from rest_framework import generics
from rest_framework.permissions import AllowAny

from .models import Corridor, Country
from .serializers import CorridorSerializer, CountrySerializer


class CountryList(generics.ListAPIView):
    """Every country the platform knows about, enabled or not.

    Disabled ones are shown to visitors as "coming soon" chips, so they are
    part of the public payload rather than filtered out."""

    permission_classes = [AllowAny]
    serializer_class = CountrySerializer
    pagination_class = None
    queryset = Country.objects.select_related("currency").all()


class CorridorList(generics.ListAPIView):
    permission_classes = [AllowAny]
    serializer_class = CorridorSerializer
    pagination_class = None
    queryset = (
        Corridor.objects.filter(is_enabled=True)
        .select_related("source__currency", "target__currency")
    )
