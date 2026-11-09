"""A root-only, transactional console for explicit user tool and branch grants."""
import hashlib
import json

from django import forms
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.http import Http404, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_protect

from .access_admin import policy_state, record_event
from .models import AgentTool, LoanAccessPolicy, LoanBranch, LoanZone, UserToolAccess, LoanAccessEvent


def state(user):
    policy = UserToolAccess.objects.filter(user=user).first()
    loan = LoanAccessPolicy.objects.filter(user=user).first()
    return {'tools': list(policy.tools.order_by('name').values_list('name', flat=True)) if policy else None,
            'tool_updated': policy.updated_at.isoformat() if policy else None,
            'tool_expiry': policy.expires_at.isoformat() if policy and policy.expires_at else None,
            'loan': policy_state(loan), 'loan_updated': loan.updated_at.isoformat() if loan else None}


def version(user):
    return hashlib.sha256(json.dumps(state(user), sort_keys=True).encode()).hexdigest()


class AccessConsoleForm(forms.Form):
    tools = forms.ModelMultipleChoiceField(queryset=AgentTool.objects.none(), required=False,
                                           widget=forms.CheckboxSelectMultiple)
    mode = forms.ChoiceField(choices=[('NONE', 'No financial data'), ('SELECTED', 'Choose branches or zones'),
                                     ('ALL', 'All active branches')], widget=forms.RadioSelect)
    branches = forms.ModelMultipleChoiceField(queryset=LoanBranch.objects.none(), required=False,
                                              widget=forms.CheckboxSelectMultiple)
    zones = forms.ModelMultipleChoiceField(queryset=LoanZone.objects.none(), required=False,
                                           widget=forms.CheckboxSelectMultiple)
    expires_at = forms.DateTimeField(required=False, widget=forms.DateTimeInput(
        attrs={'type': 'datetime-local'}, format='%Y-%m-%dT%H:%M'), label='Access ends (optional, Bangladesh time)')
    reason = forms.CharField(min_length=8, max_length=500, label='Reason for this change',
                             widget=forms.TextInput(attrs={'placeholder': 'For example: Branch reporting responsibility'}))
    version = forms.CharField(widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['tools'].queryset = AgentTool.objects.filter(is_active=True).filter(
            Q(module__isnull=True) | Q(module__is_active=True)).select_related('module').order_by('category', 'name')
        self.fields['tools'].label_from_instance = lambda t: t.display_name or t.name.replace('-', ' ').replace('_', ' ').title()
        self.fields['branches'].queryset = LoanBranch.objects.filter(is_active=True, zone__is_active=True).select_related('zone').order_by('code')
        self.fields['zones'].queryset = LoanZone.objects.filter(is_active=True).order_by('code')

    def clean(self):
        data = super().clean()
        if data.get('expires_at') and data['expires_at'] <= timezone.now():
            self.add_error('expires_at', 'Choose a future date, or leave this empty.')
        if data.get('mode') == 'SELECTED' and not data.get('branches') and not data.get('zones'):
            self.add_error('branches', 'Select at least one branch or zone.')
        if data.get('mode') in {'ALL', 'NONE'}:
            data['branches'] = LoanBranch.objects.none()
            data['zones'] = LoanZone.objects.none()
        if data.get('tools') is not None:
            from .loan_access import protected_tools
            protected = protected_tools()
            scoped = [t for t in data['tools'] if t.requires_loan_scope or t.loan_access_mode in {'FIXED', 'APP'} or t.name in protected]
            if scoped and data.get('mode') == 'NONE':
                self.add_error('mode', 'These report tools need branch access. Choose branches or remove the report tools.')
            if data.get('mode') == 'ALL':
                codes = set(self.fields['branches'].queryset.values_list('code', flat=True))
            else:
                codes = set(data.get('branches', []).values_list('code', flat=True)) if data.get('branches') is not None else set()
                if data.get('zones') is not None:
                    codes.update(self.fields['branches'].queryset.filter(zone__in=data['zones']).values_list('code', flat=True))
            if scoped and not 1 <= len(codes) <= 5000:
                self.add_error('mode', 'Select between 1 and 5000 active branches for financial reports.')
            for tool in scoped:
                if tool.loan_access_mode == 'FIXED' and (not tool.fixed_branch_codes or not set(tool.fixed_branch_codes).issubset(codes)):
                    self.add_error('tools', f'{tool.display_name or tool.name} needs every branch in its fixed endpoint scope. Choose a narrower tool or include its full scope.')
        return data


def preview(data):
    branches = LoanBranch.objects.filter(is_active=True, zone__is_active=True)
    if data['mode'] == 'SELECTED':
        branches = branches.filter(Q(pk__in=data['branches']) | Q(zone__in=data['zones']))
    elif data['mode'] == 'NONE':
        branches = branches.none()
    from .loan_access import protected_tools
    protected = protected_tools()
    codes = list(branches.order_by('code').values_list('code', flat=True))
    return {'tools': [{'title': t.display_name or t.name, 'example': t.example_question,
                       'scoped': t.requires_loan_scope or t.loan_access_mode in {'FIXED', 'APP'} or t.name in protected,
                       'approval': t.requires_approval,
                       'paused': not t.is_active or (t.module_id and not t.module.is_active)} for t in data['tools']],
            'branch_count': len(codes), 'codes': codes[:20], 'more': len(codes) > 20,
            'mode': data['mode'], 'expires_at': data.get('expires_at')}


@staff_member_required(login_url='/login/')
@csrf_protect
def user_access(request):
    if not request.user.is_superuser:
        raise PermissionDenied
    if request.method not in {'GET', 'POST'}:
        return HttpResponseNotAllowed(['GET', 'POST'])
    users = get_user_model().objects.filter(is_superuser=False).order_by('username')
    query = request.GET.get('q', '').strip()[:150]
    if query:
        users = users.filter(Q(username__icontains=query) | Q(first_name__icontains=query) | Q(last_name__icontains=query))
    target_id = request.POST.get('user') if request.method == 'POST' else request.GET.get('user')
    if target_id and (not target_id.isdecimal() or len(target_id) > 18):
        raise Http404
    target = get_object_or_404(get_user_model(), pk=target_id, is_superuser=False) if target_id and target_id.isdecimal() else None
    if request.method == 'POST' and not target:
        raise PermissionDenied
    form, summary, inherited = None, None, False
    with timezone.override('Asia/Dhaka'):
        if target:
            from .mcp_gateway import _allowed
            policy = UserToolAccess.objects.filter(user=target).first()
            loan = LoanAccessPolicy.objects.filter(user=target).first()
            inherited = policy is None
            initial = {'mode': loan.mode if loan else 'NONE', 'version': version(target),
                       'branches': loan.branches.all() if loan else [], 'zones': loan.zones.all() if loan else [],
                       'expires_at': policy.expires_at if policy else loan.expires_at if loan else None,
                       'tools': policy.tools.all() if policy else [t.pk for t in AgentTool.objects.select_related('module') if _allowed(t, target)]}
            form = AccessConsoleForm(request.POST if request.method == 'POST' else None, initial=initial)
            if request.method == 'POST' and request.POST.get('action') == 'preview':
                form.fields['reason'].required = False
            if request.method == 'POST':
                action = request.POST.get('action')
                if action not in {'preview', 'save', 'revoke'}:
                    form.add_error(None, 'Choose Preview, Save or Revoke.')
                elif action == 'revoke':
                    # Revocation does not depend on selections that may now be disabled or expired.
                    reason = request.POST.get('reason', '').strip()
                    if not 8 <= len(reason) <= 500:
                        form.add_error('reason', 'Enter a reason of at least 8 characters to revoke access.')
                    else:
                        form._console_revoke = True
                valid = getattr(form, '_console_revoke', False) or (action in {'preview', 'save'} and form.is_valid())
                if valid:
                    with transaction.atomic():
                        get_user_model().objects.select_for_update().get(pk=target.pk)
                        if request.POST.get('version') != version(target):
                            form.add_error(None, 'Access changed in another session. Reload this user before saving.')
                        elif action == 'preview':
                            summary = preview(form.cleaned_data)
                        else:
                            before = state(target)
                            explicit, _ = UserToolAccess.objects.get_or_create(user=target)
                            loan, _ = LoanAccessPolicy.objects.get_or_create(user=target)
                            revoked = action == 'revoke'
                            data = form.cleaned_data if not revoked else {}
                            explicit.expires_at = data.get('expires_at')
                            explicit.save()
                            explicit.tools.set(data.get('tools', []))
                            loan.mode = data.get('mode', 'NONE')
                            loan.expires_at = data.get('expires_at') if loan.mode != 'NONE' else None
                            loan.updated_by = request.user
                            loan.save()
                            loan.branches.set(data.get('branches', []))
                            loan.zones.set(data.get('zones', []))
                            record_event(request.user, target, 'user_access_revoked' if revoked else 'user_access_saved',
                                         reason if revoked else data['reason'], before, state(target))
                            return redirect(f'{request.path}?user={target.pk}&saved=1')
            elif policy:
                summary = preview({'tools': policy.tools.select_related('module'), 'mode': loan.mode if loan else 'NONE',
                                   'branches': loan.branches.all() if loan else [], 'zones': loan.zones.all() if loan else [],
                                   'expires_at': policy.expires_at})
        response = render(request, 'agentic_ai/user_access.html', {'people': users[:100], 'search': query,
            'target': target, 'form': form, 'summary': summary, 'inherited': inherited,
            'saved': request.GET.get('saved') == '1', 'now': timezone.now(),
            'events': LoanAccessEvent.objects.filter(subject=target).order_by('-created_at')[:5] if target else []})
    response['Cache-Control'] = 'no-store'
    return response
