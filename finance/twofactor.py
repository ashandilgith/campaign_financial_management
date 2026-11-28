"""Time-based one-time codes (TOTP, e.g. Google or Microsoft Authenticator) for sensitive roles.

``TWO_FACTOR_REQUIRED=1`` makes MD, Finance and Admin users enrol before they can do anything.
Anyone can turn it on for themselves under *Security*. An admin can reset a lost device in /admin/
(Staff profiles → untick "totp enabled").
"""
import base64
import io

import pyotp
from django.conf import settings
from django.shortcuts import redirect

from . import services as svc
from .models import StaffProfile

SESSION_KEY = "2fa_ok"
EXEMPT = ("/security/", "/logout/", "/login/", "/static/", "/healthz", "/password-reset/", "/reset/")


def profile(user):
    return StaffProfile.objects.get_or_create(user=user)[0]


def required_for(user):
    if not getattr(settings, "TWO_FACTOR_REQUIRED", False):
        return False
    return svc.is_md(user) or svc.is_finance(user) or svc.is_admin(user) or user.is_superuser


def verify(secret, code):
    return bool(secret) and pyotp.TOTP(secret).verify((code or "").replace(" ", ""), valid_window=1)


def provisioning_uri(user, secret):
    return pyotp.TOTP(secret).provisioning_uri(name=user.get_username(),
                                               issuer_name=getattr(settings, "EMAIL_ORG_NAME", "Campaign Finance"))


def qr_svg(uri):
    import qrcode
    import qrcode.image.svg
    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=8)
    buf = io.BytesIO()
    img.save(buf)
    return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode()


class TwoFactorMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated and not request.session.get(SESSION_KEY) \
                and not request.path.startswith(EXEMPT):
            p = StaffProfile.objects.filter(user=user).first()
            if p and p.totp_enabled:
                return redirect(f"/security/verify/?next={request.get_full_path()}")
            if required_for(user):
                return redirect("/security/setup/")
        return self.get_response(request)
