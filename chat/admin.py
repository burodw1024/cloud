from django.contrib import admin
from django.urls import path, reverse
from django.shortcuts import render
from django.contrib.admin.views.decorators import staff_member_required


from django.contrib.auth.models import Group, User
from django.contrib.auth.admin import GroupAdmin as BaseGroupAdmin


from .models import SystemSetting


def ai_tools_view(request):
    links = [
        {"name": "Upload JSONL", "url": reverse("upload_jsonl")},
        {"name": "Qdrant Admin", "url": "/admin-tools/qdrant/"},
        {"name": "Export Actors JSONL", "url": "/export/actors.jsonl"},
    ]
    return render(request, "admin/ai_tools.html", {"links": links})

def get_admin_urls(original_get_urls):
    def urls():
        my_urls = [
            path("ai-tools/", staff_member_required(ai_tools_view), name="ai_tools"),
        ]
        return my_urls + original_get_urls()
    return urls

admin.site.get_urls = get_admin_urls(admin.site.get_urls)
admin.site.site_header = "BURO Insight AI"
admin.site.site_title = "BURO Insight AI"






admin.site.unregister(Group)


@admin.register(Group)
class GroupAdmin(BaseGroupAdmin):
    list_display = ("name", "user_count", "users_list")
    search_fields = ("name",)

    def user_count(self, obj):
        return obj.user_set.count()
    user_count.short_description = "User Count"

    def users_list(self, obj):
        users = obj.user_set.all()[:20]
        return ", ".join([u.username for u in users]) or "-"
    users_list.short_description = "Users"



@admin.register(SystemSetting)
class SystemSettingAdmin(admin.ModelAdmin):
    list_display = ("key", "value", "is_active", "updated_at")
    search_fields = ("key", "value", "description")
    list_filter = ("is_active",)


from django.core.exceptions import PermissionDenied, ValidationError
from django.contrib import messages
from django.shortcuts import redirect
from .models import EmployeeSyncSettings, EmployeeDirectoryEntry


class EmployeeRootAdmin(admin.ModelAdmin):
    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_staff and (request.user.is_superuser or request.user.username == 'root_123')

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request)

    has_change_permission = has_view_permission

    def has_add_permission(self, request):
        return self.has_module_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(EmployeeSyncSettings)
class EmployeeSyncSettingsAdmin(EmployeeRootAdmin):
    change_form_template = 'admin/employee_sync_settings.html'
    readonly_fields = ('last_sync_at', 'last_result')

    def has_add_permission(self, request):
        return super().has_add_permission(request) and not EmployeeSyncSettings.objects.exists()

    def get_urls(self):
        return [path('<path:object_id>/sync/', self.admin_site.admin_view(self.sync_now), name='chat_employee_sync_now')] + super().get_urls()

    def sync_now(self, request, object_id):
        if not self.has_change_permission(request) or request.method != 'POST':
            raise PermissionDenied
        from .employee_sync import sync_employees
        try:
            result = sync_employees()
            self.message_user(request, result, messages.SUCCESS)
        except (ValueError, ValidationError) as exc:
            self.message_user(request, str(exc), messages.ERROR)
        return redirect('admin:chat_employeesyncsettings_change', 1)


@admin.register(EmployeeDirectoryEntry)
class EmployeeDirectoryEntryAdmin(EmployeeRootAdmin):
    list_display = ('employee_code', 'employee_name', 'branch', 'status', 'synced_at')
    search_fields = ('employee_id', 'employee_code', 'employee_name', 'branch')
    list_filter = ('status',)
    readonly_fields = ('user', 'employee_id', 'employee_code', 'employee_name', 'branch', 'status', 'synced_at')

    def has_add_permission(self, request):
        return False
