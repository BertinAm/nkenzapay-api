from django.contrib import admin
from django.urls import include, path

from nkenzapay.accounts.urls import auth_patterns, me_patterns
from nkenzapay.content import views as content_views
from nkenzapay.notifications import views as notification_views
from nkenzapay.transactions.views import (
    AttachmentUrl,
    LocalUploadView,
    TransactionListCreate,
)

api_v1 = [
    path("auth/", include((auth_patterns, "auth"))),
    path("me/", include((me_patterns, "me"))),
    path("geo/", include("nkenzapay.geo.urls")),
    path("rates/", include("nkenzapay.rates.urls")),
    path("payments/", include("nkenzapay.payments.urls")),
    # The collection sits at /transactions, the members under /transactions/.
    path("transactions", TransactionListCreate.as_view(), name="transaction-list"),
    path("transactions/", include("nkenzapay.transactions.urls")),
    path("notifications", notification_views.NotificationList.as_view()),
    path("notifications/read", notification_views.MarkRead.as_view()),
    path("notifications/preferences", notification_views.PreferencesView.as_view()),
    path("news", content_views.NewsList.as_view()),
    path("news/<slug:slug>", content_views.NewsDetail.as_view()),
    path("newsletter/subscribe", content_views.NewsletterSubscribe.as_view()),
    path("newsletter/confirm", content_views.NewsletterConfirm.as_view()),
    path("newsletter/unsubscribe", content_views.NewsletterUnsubscribe.as_view()),
    path("legal/<slug:slug>", content_views.LegalDocumentView.as_view()),
    path("support/report", content_views.SupportReport.as_view()),
    path("attachments/<int:pk>/url", AttachmentUrl.as_view()),
    path("uploads/local/<str:signed>", LocalUploadView.as_view()),
    path("admin/", include("nkenzapay.adminapi.urls")),
]

urlpatterns = [
    path("django-admin/", admin.site.urls),
    path("api/v1/", include((api_v1, "v1"))),
]
