from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.http import HttpResponse
from django.urls import include, path

urlpatterns = [
    path("healthz", lambda r: HttpResponse("ok")),
    path("admin/", admin.site.urls),
    path("login/", auth_views.LoginView.as_view(template_name="finance/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("password-reset/", auth_views.PasswordResetView.as_view(
        template_name="finance/password_reset.html", email_template_name="finance/email/password_reset.txt",
        subject_template_name="finance/email/password_reset_subject.txt"), name="password_reset"),
    path("password-reset/sent/", auth_views.PasswordResetDoneView.as_view(
        template_name="finance/password_message.html",
        extra_context={"title": "Check your email", "text": "If that address belongs to an account, a link to set a new password is on its way."}),
        name="password_reset_done"),
    path("reset/<uidb64>/<token>/", auth_views.PasswordResetConfirmView.as_view(
        template_name="finance/password_set.html"), name="password_reset_confirm"),
    path("reset/done/", auth_views.PasswordResetCompleteView.as_view(
        template_name="finance/password_message.html",
        extra_context={"title": "Password set", "text": "Your password is set. You can sign in now."}),
        name="password_reset_complete"),
    path("security/password/", auth_views.PasswordChangeView.as_view(
        template_name="finance/password_set.html", success_url="/security/"), name="password_change"),
    path("", include("finance.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
