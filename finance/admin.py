from django.contrib import admin

from . import models


class BudgetLineInline(admin.TabularInline):
    model = models.BudgetLine
    extra = 0


@admin.register(models.Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "client", "status", "start_date", "end_date")
    list_filter = ("status",)
    search_fields = ("code", "name", "client")
    inlines = [BudgetLineInline]


@admin.register(models.Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ("name", "contact_person", "phone")
    search_fields = ("name",)


@admin.register(models.PaymentRequest)
class PaymentRequestAdmin(admin.ModelAdmin):
    list_display = ("id", "campaign", "kind", "purpose", "amount", "status", "requested_by", "created_at")
    list_filter = ("status", "kind", "campaign")
    search_fields = ("purpose", "supplier__name")


@admin.register(models.AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    """Read-only: the trail must not be edited."""

    list_display = ("created_at", "actor", "action", "campaign", "request", "message")
    list_filter = ("action",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


for m in (models.SupplierContract, models.Payment, models.Settlement, models.Attachment,
          models.BudgetRevision, models.ApprovalDecision):
    admin.site.register(m)


@admin.register(models.StaffProfile)
class StaffProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "supervisor", "email_notifications", "totp_enabled")
    list_filter = ("totp_enabled",)
    exclude = ("totp_secret",)
    readonly_fields = ("totp_enabled",)
    actions = ["reset_two_factor"]

    @admin.action(description="Reset two-factor (lost phone): the person sets it up again at next sign-in")
    def reset_two_factor(self, request, queryset):
        from . import services as svc
        for p in queryset:
            p.totp_enabled, p.totp_secret = False, ""
            p.save(update_fields=["totp_enabled", "totp_secret"])
            svc.log(request.user, "user.2fa_reset", f"Two-factor reset for {p.user.username}")
        self.message_user(request, f"Two-factor reset for {queryset.count()} user(s).")


def _invite(modeladmin, request, queryset):
    from django.core.exceptions import ValidationError
    from . import services as svc
    sent, skipped = 0, []
    for u in queryset:
        try:
            svc.invite_user(request.user, u)
            sent += 1
        except ValidationError as e:
            skipped.append(f"{u.username}: {e.messages[0]}")
    modeladmin.message_user(request, f"{sent} invitation(s) sent." + (" Skipped – " + "; ".join(skipped) if skipped else ""))


_invite.short_description = "Email a set-password invitation"
from django.contrib.auth.admin import UserAdmin as _UA  # noqa: E402
from django.contrib.auth.models import User  # noqa: E402

admin.site.unregister(User)


@admin.register(User)
class UserAdmin(_UA):
    actions = [_invite]
