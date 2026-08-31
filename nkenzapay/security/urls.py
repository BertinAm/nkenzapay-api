from django.urls import path

from . import views

urlpatterns = [
    path("overview", views.SecurityOverview.as_view()),
    path("events", views.SecurityEventList.as_view()),
    path("events/<int:pk>", views.SecurityEventDetail.as_view()),
    path("blocked", views.BlockedAddressList.as_view()),
    path("block", views.BlockAddress.as_view()),
]
