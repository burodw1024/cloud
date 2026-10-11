# chat/models.py
from __future__ import annotations

import uuid
from django.conf import settings
from django.db import models
from django.utils import timezone


from django.core.cache import cache
from django.core.exceptions import ValidationError


# ✅ must be top-level (migrations can serialize it)
def gen_file_id() -> str:
    return uuid.uuid4().hex


class UserProfile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")

    is_head_office = models.BooleanField(default=False)
    branch_code = models.CharField(max_length=32, blank=True, default="")
    allowed_branch_codes = models.TextField(blank=True, default="")
    department = models.CharField(max_length=64, blank=True, default="")

    def allowed_branches_set(self):
        codes = []
        for x in (self.allowed_branch_codes or "").split(","):
            x = x.strip()
            if x:
                codes.append(x)
        if self.branch_code and self.branch_code.strip():
            bc = self.branch_code.strip()
            if bc not in codes:
                codes.append(bc)
        return set(codes)

    def __str__(self):
        return f"UserProfile({self.user.username})"


class SavedPrompt(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saved_prompts")
    title = models.CharField(max_length=120)
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("owner", "title")]
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        return f"{self.title} ({self.owner})"


class ChatSession(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="chat_sessions")
    title = models.CharField(max_length=255, blank=True, default="Chat")
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"{self.user.username} - {self.created_at}"


class ChatMessage(models.Model):
    ROLE_CHOICES = (
        ("user", "User"),
        ("assistant", "Assistant"),
        ("system", "System"),
    )

    session = models.ForeignKey(ChatSession, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    content = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"{self.role} - {self.created_at}"


class LongTermMemory(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="long_term_memories")
    content = models.TextField()
    metadata = models.JSONField(default=dict, blank=True)
    qdrant_id = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"LTM - {self.user.username}"


class UploadedFile(models.Model):
    # ✅ UUID token used by UI (stable + unique)
    file_id = models.CharField(
        max_length=64,
        unique=True,
        db_index=True,
        default=gen_file_id,
        editable=False,
    )

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True)
    filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=120, blank=True, default="")
    file = models.FileField(upload_to="uploads/%Y/%m/%d/")
    extracted_text = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.file_id} - {self.filename}"



from django.db import models
from django.contrib.auth.models import User


class CustomerSupportTicket(models.Model):
    STATUS_CHOICES = [
        ("OPEN", "Open"),
        ("IN_PROGRESS", "In Progress"),
        ("RESOLVED", "Resolved"),
        ("ESCALATED", "Escalated"),
        ("CLOSED", "Closed"),
    ]

    PRIORITY_CHOICES = [
        ("LOW", "Low"),
        ("NORMAL", "Normal"),
        ("HIGH", "High"),
        ("URGENT", "Urgent"),
    ]

    ESCALATION_CHOICES = [
        ("BRANCH", "Branch"),
        ("AREA", "Area"),
        ("REGION", "Region"),
        ("HEAD_OFFICE", "Head Office"),
    ]

    member_id = models.CharField(max_length=50, blank=True, null=True)
    mobile_no = models.CharField(max_length=20, blank=True, null=True)
    branch_code = models.CharField(max_length=30, blank=True, null=True)

    complaint_type = models.CharField(max_length=100, default="general_support")
    complaint_text = models.TextField()

    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default="OPEN")
    priority = models.CharField(max_length=30, choices=PRIORITY_CHOICES, default="NORMAL")
    escalation_level = models.CharField(max_length=30, choices=ESCALATION_CHOICES, default="BRANCH")

    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="customer_support_tickets",
    )

    assigned_to = models.CharField(max_length=100, blank=True, null=True)
    resolution_note = models.TextField(blank=True, null=True)

    source = models.CharField(max_length=50, default="DJANGO_LOCAL")
    nifi_ticket_id = models.CharField(max_length=100, blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def can_be_seen_by(self, user):
        if not user or not user.is_authenticated:
            return False

        if user.is_superuser:
            return True

        group_names = set(user.groups.values_list("name", flat=True))

        if "customer_support_admin" in group_names:
            return True

        if "customer_support_head_office" in group_names:
            return True

        if "customer_support_area" in group_names:
            return True

        if "customer_support_branch" in group_names:
            return True

        if self.created_by_id == user.id:
            return True

        return False

    def __str__(self):
        return f"Ticket #{self.id} - {self.complaint_type} - {self.status}"
#adding system settings 


class SystemSetting(models.Model):
    key = models.CharField(max_length=120, unique=True)
    value = models.TextField(blank=True, default="")
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "System Setting"
        verbose_name_plural = "System Settings"
        ordering = ["key"]

    def __str__(self):
        return self.key

    def clean(self):
        url_keys = [
            "QDRANT_URL",
            "QDRANT_PRIMARY_URL",
            "QDRANT_SECONDARY_URL",
            "OLLAMA_URL",
            "NIFI_BASE_URL",
            "NIFI_CUSTOMER_SUPPORT_URL",
            "NIFI_HR_URL",
            "NIFI_CORE_URL",
            "NIFI_CASHFLOW_URL",
        ]

        if self.key in url_keys:
            value = (self.value or "").strip()
            if value and not value.startswith(("http://", "https://")):
                raise ValidationError(
                    f"{self.key} must start with http:// or https://"
                )

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)
        cache.delete(f"system_setting:{self.key}")

    def delete(self, *args, **kwargs):
        cache.delete(f"system_setting:{self.key}")
        return super().delete(*args, **kwargs)
