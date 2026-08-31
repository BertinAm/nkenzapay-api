from django.urls import path

from . import views

urlpatterns = [
    path("countries", views.CountryList.as_view(), name="country-list"),
    path("corridors", views.CorridorList.as_view(), name="corridor-list"),
]
