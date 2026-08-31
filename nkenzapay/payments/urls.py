from django.urls import path

from . import views

urlpatterns = [
    path("methods", views.PaymentMethodList.as_view(), name="payment-methods"),
]
