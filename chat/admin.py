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
