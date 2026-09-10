from django.urls import include, path

from . import twofactor, verification, views

urlpatterns = [
    path("overview", views.Overview.as_view()),
    path("badges", views.Badges.as_view()),

    path("2fa", twofactor.TwoFactorStatus.as_view()),
    path("2fa/setup", twofactor.TwoFactorSetup.as_view()),
    path("2fa/confirm", twofactor.TwoFactorConfirm.as_view()),

    path("verifications", verification.VerificationQueue.as_view()),
    path("verifications/<int:pk>", verification.VerificationDetail.as_view()),
    path("verifications/<int:pk>/<str:action>",
         verification.VerificationDecide.as_view()),

    path("transactions", views.AdminTransactionList.as_view()),
    path("transactions/<str:reference>", views.AdminTransactionDetail.as_view()),
    path("transactions/<str:reference>/messages", views.AdminReply.as_view()),
    path("transactions/<str:reference>/<str:action>", views.AdminTransactionAction.as_view()),

    path("messages/inbox", views.AdminInbox.as_view()),
    # Threads are per customer. Kept above the users routes, which end in a
    # catch-all <str:action> that would otherwise swallow these.
    path("customers/<int:pk>/messages", views.AdminCustomerThread.as_view()),
    path("customers/<int:pk>/attachments/upload-url",
         views.AdminThreadUploadUrl.as_view()),
    path("customers/<int:pk>/attachments", views.AdminThreadAttachment.as_view()),

    path("users", views.AdminUserList.as_view()),
    path("users/<int:pk>", views.AdminUserDetail.as_view()),
    path("users/<int:pk>/login-activity", views.AdminUserLoginActivity.as_view()),
    path("users/<int:pk>/<str:action>", views.AdminUserSuspend.as_view()),

    path("settings/rates", views.RateSettings.as_view()),
    path("settings/fees", views.FeeSettings.as_view()),
    path("settings/limits", views.LimitSettings.as_view()),
    path("settings/company", views.CompanySettings.as_view()),
    path("settings/accounts", views.AdminAccounts.as_view()),

    path("payment-methods", views.AdminPaymentMethods.as_view()),
    path("payment-methods/<int:pk>", views.AdminPaymentMethods.as_view()),

    path("countries", views.AdminCountries.as_view()),
    path("countries/<str:iso2>", views.AdminCountries.as_view()),

    path("news", views.AdminNewsList.as_view()),
    path("news/<int:pk>", views.AdminNewsDetail.as_view()),

    path("disputes", views.AdminDisputeList.as_view()),
    path("disputes/<int:pk>/resolve", views.AdminDisputeResolve.as_view()),

    path("notifications", views.AdminNotifications.as_view()),
    path("notifications/rules", views.AdminDeliveryRules.as_view()),

    path("analytics/<str:family>", views.Analytics.as_view()),

    path("exports", views.ExportCreate.as_view()),
    path("exports/<int:pk>", views.ExportDetail.as_view()),
    path("exports/<int:pk>/download", views.export_download),

    path("audit", views.AdminAudit.as_view()),

    path("security/", include("nkenzapay.security.urls")),
]
