from django.db.models import Q
from rest_framework import generics
from rest_framework.permissions import AllowAny

from .models import Corridor, Country
from .serializers import CorridorSerializer, CountrySerializer


class CountryList(generics.ListAPIView):
    """The countries the platform trades with, or intends to.

    Disabled ones are shown to visitors as "coming soon" chips, so they are
    part of the public payload rather than filtered out. What is filtered out
    is the two hundred that exist only so somebody can say where they live:
    those are not coming soon and saying so would be a promise.

    ?scope=all returns them too, for the one screen that needs it — signing up,
    where refusing somebody for living in the wrong place is the opposite of
    the point.
    """

    permission_classes = [AllowAny]
    serializer_class = CountrySerializer
    pagination_class = None

    def get_queryset(self):
        countries = Country.objects.select_related("currency")
        if self.request.query_params.get("scope") == "all":
            return countries
        return countries.filter(
            Q(is_enabled=True) | Q(is_origin=True) | Q(is_destination=True)
        )


class CorridorList(generics.ListAPIView):
    permission_classes = [AllowAny]
    serializer_class = CorridorSerializer
    pagination_class = None
    queryset = (
        Corridor.objects.filter(is_enabled=True)
        .select_related("source__currency", "target__currency")
    )
