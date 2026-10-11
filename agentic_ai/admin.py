from django.contrib import admin
from .models import AgentTool, AgentRunLog, AgentApprovalRequest


@admin.register(AgentTool)
class AgentToolAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active", "requires_approval", "created_at")
    search_fields = ("name", "description")
    list_filter = ("is_active", "requires_approval")


@admin.register(AgentRunLog)
class AgentRunLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "detected_intent", "selected_tool", "status", "risk_level")
    search_fields = ("user_query", "final_answer", "selected_tool", "detected_intent")
    list_filter = ("status", "risk_level", "selected_tool")
    readonly_fields = ("created_at",)


@admin.register(AgentApprovalRequest)
class AgentApprovalRequestAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "tool_name", "action_type", "status", "approved_by")
    search_fields = ("tool_name", "action_type")
    list_filter = ("status", "tool_name")
    readonly_fields = ("created_at", "updated_at")
