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

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.action_type == "mcp_tool_call":
            # MCP approvals must execute through the reviewed API, not a status edit.
            return self.readonly_fields + ("user", "action_type", "tool_name", "request_payload", "status", "approved_by", "response_note")
        return self.readonly_fields
