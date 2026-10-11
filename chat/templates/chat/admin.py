from django.contrib import admin
from .models import UserProfile

@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "home_branch_code", "allowed_branches_csv")
    search_fields = ("user__username", "home_branch_code")

