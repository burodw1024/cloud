from django.db import models
from django.core.exceptions import ValidationError
from django.contrib.auth.models import User


class AgentTool(models.Model):
    name = models.CharField(max_length=150, unique=True)
    description = models.TextField(blank=True)
    display_name = models.CharField(max_length=150, blank=True)
    category = models.CharField(max_length=80, default="General")
    example_question = models.TextField(blank=True, max_length=2000)
    is_active = models.BooleanField(default=True)
    requires_approval = models.BooleanField(default=False)
    allowed_roles = models.JSONField(default=list, blank=True)

    def clean(self):
        super().clean()
        if self.name != self.name.strip() or not self.name.strip():
            raise ValidationError({"name": "Use the exact nonempty gateway tool name without surrounding whitespace."})
        roles = self.allowed_roles
        if not isinstance(roles, list) or any(not isinstance(role, str) or not role.strip() or role != role.strip() for role in roles):
            raise ValidationError({"allowed_roles": 'Use a JSON list of exact group names, for example ["manager", "cashflow"].'})

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class AgentRunLog(models.Model):
    STATUS_CHOICES = [
        ("success", "Success"),
        ("failed", "Failed"),
        ("blocked", "Blocked"),
        ("approval_required", "Approval Required"),
    ]

    RISK_CHOICES = [
        ("low", "Low"),
        ("medium", "Medium"),
        ("high", "High"),
    ]

    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    user_query = models.TextField()
    detected_intent = models.CharField(max_length=150, blank=True, null=True)
    selected_tool = models.CharField(max_length=150, blank=True, null=True)
    tool_input = models.JSONField(default=dict, blank=True)
    tool_output_summary = models.TextField(blank=True, null=True)
    usage_summary = models.JSONField(default=dict, blank=True)
    final_answer = models.TextField(blank=True, null=True)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default="success")
    risk_level = models.CharField(max_length=50, choices=RISK_CHOICES, default="low")
    error_message = models.TextField(blank=True, null=True)
    session_id = models.CharField(max_length=200, blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.selected_tool or 'no-tool'} - {self.status}"


class AgentApprovalRequest(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
    ]

    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agentic_requests",
    )
    action_type = models.CharField(max_length=150)
    tool_name = models.CharField(max_length=150)
    request_payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default="pending")

    approved_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_agentic_requests",
    )

    response_note = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.tool_name} - {self.status}"
