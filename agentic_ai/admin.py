from django.contrib import admin
from .models import AgentTool, AgentRunLog, AgentApprovalRequest, BusinessModule


@admin.register(BusinessModule)
class BusinessModuleAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'is_active', 'sort_order')
    list_filter = ('is_active',)
    search_fields = ('name', 'description')
    prepopulated_fields = {'slug': ('name',)}


@admin.register(AgentTool)
class AgentToolAdmin(admin.ModelAdmin):
    list_display = ("name", "display_name", "module", "category", "is_active", "requires_approval", "created_at")
    search_fields = ("name", "display_name", "description", "category")
    list_filter = ("module", "category", "is_active", "requires_approval")


@admin.register(AgentRunLog)
class AgentRunLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "detected_intent", "selected_tool", "status", "risk_level")
    search_fields = ("user__username", "user_query", "final_answer", "error_message", "selected_tool", "detected_intent")
    list_filter = ("status", "risk_level", "selected_tool")
    readonly_fields = tuple(field.name for field in AgentRunLog._meta.fields)
    list_select_related = ("user",)
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AgentApprovalRequest)
class AgentApprovalRequestAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "tool_name", "action_type", "status", "approved_by")
    search_fields = ("tool_name", "action_type")
    list_filter = ("status", "tool_name")
    readonly_fields = ("created_at", "updated_at")
