from django.conf import settings
from django.db import models
from django.utils import timezone


class WorkflowDefinition(models.Model):
    name = models.CharField(max_length=100, unique=True)
    title = models.CharField(max_length=200)
    version = models.IntegerField(default=1)
    is_active = models.BooleanField(default=True)
    allowed_groups = models.JSONField(default=list, blank=True)
    definition_json = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} v{self.version}"


class WorkflowSession(models.Model):
    STATUS_CHOICES = (
        ("active", "Active"),
        ("waiting_upload", "Waiting Upload"),
        ("handoff", "Handoff"),
        ("completed", "Completed"),
        ("cancelled", "Cancelled"),
        ("failed", "Failed"),
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="workflow_sessions"
    )
    session_id = models.CharField(max_length=100, db_index=True)
    intent_name = models.CharField(max_length=100, db_index=True)
    definition = models.ForeignKey(
        "workflows.WorkflowDefinition",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="workflow_sessions"
    )
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default="active")
    current_step = models.CharField(max_length=100, blank=True, null=True)
    collected_data = models.JSONField(default=dict, blank=True)
    last_question = models.TextField(blank=True, null=True)
    submitted_to_backend = models.BooleanField(default=False)
    backend_reference = models.CharField(max_length=200, blank=True, null=True)
    handoff_requested = models.BooleanField(default=False)
    started_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(blank=True, null=True)

    def __str__(self):
        return f"{self.session_id} - {self.intent_name} - {self.status}"


class WorkflowStepAnswer(models.Model):
    workflow = models.ForeignKey(
        "workflows.WorkflowSession",
        on_delete=models.CASCADE,
        related_name="answers"
    )
    step_name = models.CharField(max_length=100)
    field_name = models.CharField(max_length=100)
    answer_value = models.TextField(blank=True, null=True)
    answer_json = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("workflow", "step_name")

    def __str__(self):
        return f"{self.workflow_id} - {self.step_name}"


class WorkflowAttachment(models.Model):
    FILE_TYPE_CHOICES = (
        ("nid_front", "NID Front"),
        ("nid_back", "NID Back"),
        ("photo", "Photo"),
        ("other", "Other"),
    )

    workflow = models.ForeignKey(
        "workflows.WorkflowSession",
        on_delete=models.CASCADE,
        related_name="attachments"
    )
    step_name = models.CharField(max_length=100, blank=True, null=True)
    field_name = models.CharField(max_length=100, blank=True, null=True)
    file_type = models.CharField(max_length=50, choices=FILE_TYPE_CHOICES, default="other")
    file = models.FileField(upload_to="workflow_uploads/%Y/%m/%d/")
    original_name = models.CharField(max_length=255, blank=True, null=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.workflow_id} - {self.file_type}"


class HumanHandoffRequest(models.Model):
    STATUS_CHOICES = (
        ("pending", "Pending"),
        ("assigned", "Assigned"),
        ("resolved", "Resolved"),
        ("closed", "Closed"),
    )

    workflow = models.ForeignKey(
        "workflows.WorkflowSession",
        on_delete=models.CASCADE,
        related_name="handoffs"
    )
    reason = models.TextField(blank=True, null=True)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default="pending")
    assigned_to = models.CharField(max_length=100, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(blank=True, null=True)

    def __str__(self):
        return f"Handoff {self.workflow_id} - {self.status}"


class AccountOpeningRequest(models.Model):
    workflow = models.OneToOneField(
        "workflows.WorkflowSession",
        on_delete=models.CASCADE,
        related_name="account_opening_request"
    )
    full_name = models.CharField(max_length=255)
    mobile = models.CharField(max_length=20)
    nid = models.CharField(max_length=50)
    address = models.TextField()
    account_type = models.CharField(max_length=50)
    nid_front_path = models.CharField(max_length=500, blank=True, null=True)
    photo_path = models.CharField(max_length=500, blank=True, null=True)
    status = models.CharField(max_length=50, default="submitted")
    submitted_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return self.full_name
