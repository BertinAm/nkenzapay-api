from django.urls import path

from . import views

auth_patterns = [
    path("register", views.RegisterView.as_view(), name="auth-register"),
    path("login", views.LoginView.as_view(), name="auth-login"),
    path("logout", views.LogoutView.as_view(), name="auth-logout"),
    path("session", views.SessionView.as_view(), name="auth-session"),
    path("password/change", views.PasswordChangeView.as_view(), name="auth-password-change"),
    path("password/reset", views.PasswordResetRequestView.as_view(), name="auth-password-reset"),
    path("password/reset/confirm", views.PasswordResetConfirmView.as_view(),
         name="auth-password-reset-confirm"),
    path("verify-email", views.VerifyEmailView.as_view(), name="auth-verify-email"),
]

me_patterns = [
    path("", views.MeView.as_view(), name="me"),
    path("profile", views.MeProfileView.as_view(), name="me-profile"),
    path("photo/upload-url", views.ProfilePhotoUploadUrlView.as_view(), name="me-photo-url"),
    path("photo/commit", views.ProfilePhotoCommitView.as_view(), name="me-photo-commit"),
    path("stats", views.my_stats, name="me-stats"),
    path("login-activity", views.LoginActivityView.as_view(), name="me-login-activity"),
]
