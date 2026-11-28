from . import services as svc
from .models import BudgetRevision, OutboundEmail, PaymentRequest, Settlement, StaffProfile


def roles(request):
    user = request.user
    if not user.is_authenticated:
        return {}
    ctx = {
        "is_md": svc.is_md(user),
        "is_finance": svc.is_finance(user),
        "is_admin": svc.is_admin(user),
        "can_manage": svc.can_manage_campaigns(user),
        "user_roles": sorted(svc.roles_of(user)),
    }
    team = list(StaffProfile.objects.filter(supervisor=user).values_list("user_id", flat=True))
    ctx["is_supervisor"] = bool(team)
    if team:
        ctx["supervisor_queue_count"] = PaymentRequest.objects.filter(
            status=PaymentRequest.Status.ENDORSE, requested_by_id__in=team).count()
    ctx["inbox_count"] = OutboundEmail.objects.filter(recipients=user).count()
    if ctx["is_md"]:
        ctx["md_queue_count"] = (
            PaymentRequest.objects.filter(status=PaymentRequest.Status.SUBMITTED).exclude(requested_by=user).count()
            + BudgetRevision.objects.filter(status=BudgetRevision.Status.PENDING).count()
        )
    if ctx["is_finance"]:
        ctx["finance_queue_count"] = (
            PaymentRequest.objects.filter(status=PaymentRequest.Status.APPROVED).count()
            + Settlement.objects.filter(status=Settlement.Status.SUBMITTED).count()
        )
    return ctx
