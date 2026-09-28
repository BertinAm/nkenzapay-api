from django.conf import settings
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
    # <path:> and not <str:>. A signed storage token carries the key, and a key
    # is a directory path: "profiles/7/2026/09/<hash>.jpg:<stamp>:<sig>". The
    # str converter stops at the first slash, so every upload and every signed
    # read 404ed before reaching the view.
    path("uploads/local/<path:signed>", LocalUploadView.as_view()),
    path("admin/", include("nkenzapay.adminapi.urls")),
]

urlpatterns = [
    path("api/v1/", include((api_v1, "v1"))),
]

# Django's own admin, in development only.
#
# It used to be routed unconditionally, which put a second way in beside the
# desk's — and a weaker one. It takes an email and a password, where every desk
# endpoint that moves money also demands TOTP. Worse, nothing was watching it:
# failed sign-ins are recorded by the API login view, not by a signal, and the
# throttles are DRF's, which Django's admin views never pass through. So it was
# an unmetered, unlogged password prompt on the host that owns the database,
# and the first URL any scanner tries.
#
# The desk runs on /api/v1/admin/ and has never needed this. Anything only
# reachable here — corridors, currencies — belongs on the desk's own screens or
# in a management command.
if settings.DEBUG:
    from django.contrib import admin

    urlpatterns.append(path("django-admin/", admin.site.urls))
