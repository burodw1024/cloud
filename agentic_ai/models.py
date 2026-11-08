import re

from django.db import models
from django.core.exceptions import ValidationError
from django.contrib.auth.models import User
from django.core.validators import RegexValidator


scope_code = RegexValidator(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$', 'Use an exact code of up to 32 letters, digits, dots, underscores or hyphens.')


class LoanZone(models.Model):
    division_code = models.CharField(max_length=32, blank=True, validators=[scope_code])
    division_name = models.CharField(max_length=150, blank=True)
    code = models.CharField(max_length=32, unique=True, validators=[scope_code])
    name = models.CharField(max_length=150)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['code']

    def __str__(self):
        return f'{self.code} · {self.name}'


class LoanBranch(models.Model):
    area_code = models.CharField(max_length=32, blank=True, validators=[scope_code])
    area_name = models.CharField(max_length=150, blank=True)
    code = models.CharField(max_length=32, unique=True, validators=[scope_code])
    name = models.CharField(max_length=150)
    zone = models.ForeignKey(LoanZone, on_delete=models.PROTECT, related_name='branches')
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['code']

    def __str__(self):
        return f'{self.code} · {self.name}'


class LoanAccessPolicy(models.Model):
    MODES = [('NONE', 'No loan data access'), ('SELECTED', 'Selected branches and/or zones'),
             ('ALL', 'All active branches and zones')]
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='loan_access_policy')
    mode = models.CharField(max_length=10, choices=MODES, default='NONE')
    branches = models.ManyToManyField(LoanBranch, blank=True)
    zones = models.ManyToManyField(LoanZone, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    updated_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name='+')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        permissions = [('manage_loan_access', 'Manage user loan data access'),
                       ('grant_all_loan_access', 'Grant all-branch loan data access'),
                       ('manage_loan_directory', 'Manage loan branch and zone directory')]

    def __str__(self):
        return f'{self.user} · {self.get_mode_display()}'


class LoanAccessEvent(models.Model):
    actor = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name='+')
    subject = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    actor_label = models.CharField(max_length=150)
    subject_label = models.CharField(max_length=150, blank=True)
    action = models.CharField(max_length=40)
    reason = models.CharField(max_length=500)
    before = models.JSONField(default=dict)
    after = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-pk']


class BusinessModule(models.Model):
    name = models.CharField(max_length=100)
    slug = models.SlugField(max_length=100, unique=True, allow_unicode=True)
    description = models.TextField(blank=True, max_length=1500)
    starter_questions = models.TextField(blank=True, max_length=10000)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['sort_order', 'name', 'pk']

    def __str__(self):
        return self.name


class AgentTool(models.Model):
    module = models.ForeignKey(BusinessModule, null=True, blank=True,
                               on_delete=models.SET_NULL, related_name='tools')
    name = models.CharField(max_length=150, unique=True)
    description = models.TextField(blank=True)
    display_name = models.CharField(max_length=150, blank=True)
    category = models.CharField(max_length=80, default="General")
    example_question = models.TextField(blank=True, max_length=2000)
    is_active = models.BooleanField(default=True)
    requires_approval = models.BooleanField(default=False)
    requires_loan_scope = models.BooleanField(default=False)
    loan_access_mode = models.CharField(max_length=10, default="SIGNED",
        choices=[("SIGNED", "Per-user signed access"), ("FIXED", "Fixed NiFi branch scope")])
    fixed_branch_codes = models.JSONField(default=list, blank=True,
        help_text="Exact full branch allowlist enforced by this tool's NiFi endpoint.")

    allowed_roles = models.JSONField(default=list, blank=True)

    def clean(self):
        super().clean()
        if self.name != self.name.strip() or not self.name.strip():
            raise ValidationError({"name": "Use the exact nonempty gateway tool name without surrounding whitespace."})
        codes = self.fixed_branch_codes
        if (not isinstance(codes, list) or len(codes) > 1000
                or any(not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,31}', code) for code in codes)
                or len(set(codes)) != len(codes)):
            raise ValidationError({'fixed_branch_codes': 'Use up to 1000 distinct exact branch codes as strings.'})
        if self.loan_access_mode == 'FIXED' and not codes:
            raise ValidationError({'fixed_branch_codes': 'Enter the full branch scope enforced by the NiFi endpoint.'})
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


class LoanDirectoryConnection(models.Model):
    """Singleton integration configuration; token ciphertext is never rendered."""
    url = models.URLField(max_length=2048)
    allow_http = models.BooleanField(default=False)
    encrypted_token = models.TextField()
    updated_at = models.DateTimeField(auto_now=True)


class UserToolAccess(models.Model):
    """An explicit selection replaces group-derived tool access for this user."""
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='tool_access_policy')
    tools = models.ManyToManyField(AgentTool, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
