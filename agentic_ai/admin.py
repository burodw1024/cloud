from django.contrib import admin
from .models import (AgentTool, AgentRunLog, AgentApprovalRequest, BusinessModule,
                     LoanZone, LoanBranch, LoanAccessPolicy, LoanAccessEvent)


class ReadOnlyAccessAdmin(admin.ModelAdmin):
    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields] + [field.name for field in self.model._meta.many_to_many]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(LoanZone)
class LoanZoneAdmin(ReadOnlyAccessAdmin):
    list_display = ('code', 'name', 'is_active')
    search_fields = ('code', 'name')


@admin.register(LoanBranch)
class LoanBranchAdmin(ReadOnlyAccessAdmin):
    list_display = ('code', 'name', 'zone', 'is_active')
    search_fields = ('code', 'name')
    list_filter = ('zone', 'is_active')
    list_select_related = ('zone',)


@admin.register(LoanAccessPolicy)
class LoanAccessPolicyAdmin(ReadOnlyAccessAdmin):
    list_display = ('user', 'mode', 'expires_at', 'updated_by', 'updated_at')
    list_filter = ('mode',)
    search_fields = ('user__username',)
    list_select_related = ('user', 'updated_by')


@admin.register(LoanAccessEvent)
class LoanAccessEventAdmin(ReadOnlyAccessAdmin):
    list_display = ('created_at', 'actor_label', 'subject_label', 'action', 'reason')
    list_filter = ('action',)
    search_fields = ('actor_label', 'subject_label', 'reason')
    list_select_related = ('actor', 'subject')
    date_hierarchy = 'created_at'


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
    list_filter = ("module", "category", "is_active", "requires_approval", "requires_loan_scope", "loan_access_mode")


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
