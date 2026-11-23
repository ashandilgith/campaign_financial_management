from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("approvals/", views.md_queue, name="md_queue"),
    path("finance/", views.finance_queue, name="finance_queue"),
    path("requests/", views.request_list, name="request_list"),
    path("requests/<int:pk>/", views.request_detail, name="request_detail"),
    path("requests/<int:pk>/<str:action>/", views.request_action, name="request_action"),
    path("attachments/<int:pk>/", views.attachment_download, name="attachment_download"),
    path("campaigns/", views.campaign_list, name="campaign_list"),
    path("campaigns/new/", views.campaign_create, name="campaign_create"),
    path("campaigns/<int:pk>/", views.campaign_detail, name="campaign_detail"),
    path("campaigns/<int:pk>/lines/", views.campaign_add_line, name="campaign_add_line"),
    path("campaigns/<int:pk>/status/", views.campaign_status, name="campaign_status"),
    path("campaigns/<int:pk>/report/", views.campaign_report, name="campaign_report"),
    path("campaigns/<int:pk>/payments.csv", views.campaign_export, name="campaign_export"),
    path("campaigns/<int:campaign_id>/request/", views.request_new, name="request_new"),
    path("lines/<int:pk>/revise/", views.line_revise, name="line_revise"),
    path("revisions/<int:pk>/decide/", views.revision_decide, name="revision_decide"),
    path("audit/", views.audit_log, name="audit_log"),
    path("ask/", views.ask_view, name="ask"),
]
