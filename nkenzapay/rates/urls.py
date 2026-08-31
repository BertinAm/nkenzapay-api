from django.urls import path

from . import views

urlpatterns = [
    path("quote", views.QuoteView.as_view(), name="rate-quote"),
    path("ticker", views.TickerView.as_view(), name="rate-ticker"),
    path("history", views.RateHistoryView.as_view(), name="rate-history"),
]
