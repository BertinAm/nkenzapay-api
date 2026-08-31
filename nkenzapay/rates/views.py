from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from nkenzapay.pricing.engine import build_quote, persist_quote
from nkenzapay.rates.providers import RateUnavailable

from .models import RateSnapshot
from .serializers import QuoteRequestSerializer, QuoteResultSerializer


class QuoteView(APIView):
    """Price a transfer.

    Open to visitors so the calculator works before anyone signs in — creating
    the order is what needs an account, not asking the price. A limit breach
    comes back as a 200 with errors attached rather than a 400, because the
    figure is still worth showing while the message sits under the field.
    """

    permission_classes = [AllowAny]
    throttle_scope = "quote"

    def post(self, request):
        form = QuoteRequestSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        data = form.validated_data

        try:
            result = build_quote(
                corridor=data["corridor_obj"],
                direction=data["direction"],
                send_amount=data["send_amount"],
                user=request.user if request.user.is_authenticated else None,
            )
        except RateUnavailable as exc:
            return Response(
                {"error": {
                    "code": "rate_unavailable",
                    "message": "Live rates are briefly unavailable. Try again in a moment.",
                    "detail": {"reason": str(exc)},
                }},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        payload = QuoteResultSerializer(result).data

        # Only a priced result a customer could act on is written down. An
        # amount under the minimum produces a figure to look at, not a quote to
        # hold, so it never reaches the database.
        if result.is_valid and request.user.is_authenticated:
            quote = persist_quote(result, user=request.user)
            payload["reference"] = quote.reference

        return Response(payload)


class TickerView(APIView):
    """The public rate strip on the marketing page."""

    permission_classes = [AllowAny]

    def get(self, request):
        pairs = []
        seen = set()
        recent = (
            RateSnapshot.objects.select_related("base", "quote")
            .order_by("base_id", "quote_id", "-fetched_at")
        )
        for snapshot in recent:
            key = (snapshot.base_id, snapshot.quote_id)
            if key in seen:
                continue
            seen.add(key)
            pairs.append({
                "base": snapshot.base_id,
                "quote": snapshot.quote_id,
                "rate": str(snapshot.effective_rate.normalize()),
                "display": (
                    f"1 {snapshot.base_id} = "
                    f"{snapshot.effective_rate.normalize():f} {snapshot.quote_id}"
                ),
                "fetched_at": snapshot.fetched_at,
            })
        return Response({"pairs": pairs, "as_of": timezone.now()})


class RateHistoryView(APIView):
    """Hourly points behind the sparkline on the customer dashboard."""

    permission_classes = [AllowAny]

    def get(self, request):
        base = request.query_params.get("base", "XAF").upper()
        quote = request.query_params.get("quote", "INR").upper()
        since = timezone.now() - timezone.timedelta(hours=24)
        rows = (
            RateSnapshot.objects.filter(base_id=base, quote_id=quote, fetched_at__gte=since)
            .order_by("fetched_at")
            .values("fetched_at", "effective_rate")
        )
        points = [{"at": r["fetched_at"], "rate": str(r["effective_rate"])} for r in rows]
        change = None
        if len(points) >= 2:
            first = float(points[0]["rate"])
            last = float(points[-1]["rate"])
            if first:
                change = round((last - first) / first * 100, 2)
        return Response({"base": base, "quote": quote, "points": points,
                         "change_percent": change})
