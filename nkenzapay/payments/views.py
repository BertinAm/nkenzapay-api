from rest_framework import generics
from rest_framework.permissions import AllowAny

from .models import PaymentMethod
from .serializers import PaymentMethodSerializer


class PaymentMethodList(generics.ListAPIView):
    """Enabled methods for one country and side. Disabled ones never appear."""

    permission_classes = [AllowAny]
    serializer_class = PaymentMethodSerializer
    pagination_class = None

    def get_queryset(self):
        queryset = PaymentMethod.objects.filter(is_enabled=True).select_related("instruction")
        country = self.request.query_params.get("country")
        side = self.request.query_params.get("side")
        if country:
            queryset = queryset.filter(country_id=country.upper())
        if side:
            queryset = queryset.filter(side=side)
        return queryset
