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
