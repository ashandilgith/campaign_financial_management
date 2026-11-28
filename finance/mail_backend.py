"""Django email backend that routes every Django-generated email (password reset, invitations)
through the same provider as the notifications: simulated inbox, Outlook (Graph) or SMTP.
"""
from django.contrib.auth import get_user_model
from django.core.mail.backends.base import BaseEmailBackend
from django.db import transaction


class OutboxEmailBackend(BaseEmailBackend):
    def send_messages(self, email_messages):
        from . import notify
        from .models import OutboundEmail

        sent = 0
        for m in email_messages:
            html = next((c for c, t in getattr(m, "alternatives", []) if t == "text/html"), "")
            e = OutboundEmail.objects.create(event="auth", to=list(m.to), cc=list(m.cc), subject=m.subject,
                                             body_text=m.body, body_html=html or "", provider=notify.provider(),
                                             status=OutboundEmail.Status.SIMULATED)
            users = list(get_user_model().objects.filter(email__in=m.to))
            if users:
                e.recipients.set(users)
            transaction.on_commit(lambda pk=e.pk: notify.deliver(pk))
            sent += 1
        return sent
