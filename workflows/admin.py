from django.contrib import admin
from .models import (
    WorkflowDefinition,
    WorkflowSession,
    WorkflowStepAnswer,
    WorkflowAttachment,
    HumanHandoffRequest,
    AccountOpeningRequest,
)


@admin.register(WorkflowDefinition)
class WorkflowDefinitionAdmin(admin.ModelAdmin):
    list_display = ("name", "title", "version", "is_active", "updated_at")
    search_fields = ("name", "title")
    list_filter = ("is_active",)


@admin.register(WorkflowSession)
class WorkflowSessionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "session_id",
        "intent_name",
        "status",
        "current_step",
        "submitted_to_backend",
        "updated_at",
    )
    search_fields = ("session_id", "intent_name", "backend_reference")
    list_filter = ("status", "intent_name")


@admin.register(WorkflowStepAnswer)
class WorkflowStepAnswerAdmin(admin.ModelAdmin):
    list_display = ("workflow", "step_name", "field_name", "updated_at")


@admin.register(WorkflowAttachment)
class WorkflowAttachmentAdmin(admin.ModelAdmin):
    list_display = ("workflow", "file_type", "original_name", "uploaded_at")


@admin.register(HumanHandoffRequest)
class HumanHandoffRequestAdmin(admin.ModelAdmin):
    list_display = ("workflow", "status", "assigned_to", "created_at")


@admin.register(AccountOpeningRequest)
class AccountOpeningRequestAdmin(admin.ModelAdmin):
    list_display = ("full_name", "mobile", "account_type", "status", "submitted_at")
