"""User data entitlements and the authoritative branch/zone directory."""
import csv
import io
import os
from itertools import islice

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import permission_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction, IntegrityError, OperationalError
from django.http import Http404, HttpResponse, HttpResponseNotAllowed
from django.shortcuts import render, redirect
from django.utils import timezone
from django.views.decorators.csrf import csrf_protect

from .models import LoanAccessPolicy, LoanAccessEvent, LoanZone, LoanBranch


class AccessForm(forms.ModelForm):
    reason = forms.CharField(min_length=8, max_length=500, widget=forms.Textarea(attrs={'rows': 2}),
                             help_text='Record the business justification or approval reference.')
    version = forms.CharField(widget=forms.HiddenInput)

    class Meta:
        model = LoanAccessPolicy
        fields = ('user', 'mode', 'zones', 'branches', 'expires_at')
        widgets = {'zones': forms.SelectMultiple(attrs={'size': 5}),
                   'branches': forms.SelectMultiple(attrs={'size': 7}),
                   'expires_at': forms.DateTimeInput(attrs={'type': 'datetime-local'}, format='%Y-%m-%dT%H:%M')}
        help_texts = {'expires_at': 'Optional. Bangladesh time (Asia/Dhaka). Access stops at this time.',
                      'zones': 'All active branches in these zones are included, including future additions.',
                      'branches': 'Additional selected branches. Hold Ctrl/Command to select multiple items.'}

    def __init__(self, *args, actor, **kwargs):
        self.actor = actor
        super().__init__(*args, **kwargs)
        self.initial['version'] = self.instance.updated_at.isoformat() if self.instance.pk else 'new'
        self.fields['user'].queryset = get_user_model().objects.order_by('username')
        if self.instance.pk:
            self.fields['user'].disabled = True
        else:
            self.fields['user'].queryset = self.fields['user'].queryset.filter(loan_access_policy__isnull=True)
        self.fields['zones'].queryset = LoanZone.objects.all()
        self.fields['branches'].queryset = LoanBranch.objects.select_related('zone').all()
        if not actor.has_perm('agentic_ai.grant_all_loan_access'):
            self.fields['mode'].choices = [choice for choice in LoanAccessPolicy.MODES if choice[0] != 'ALL']

    def clean(self):
        data = super().clean()
        user = data.get('user')
        if user and user.pk == self.actor.pk and not self.actor.is_superuser:
            raise forms.ValidationError('Delegated access administrators cannot change their own access. Ask another authorized administrator.')
        mode, branches, zones = data.get('mode'), data.get('branches'), data.get('zones')
        if mode == 'ALL' and not self.actor.has_perm('agentic_ai.grant_all_loan_access'):
            raise forms.ValidationError('You do not have permission to grant all-branch access.')
        if mode == 'SELECTED' and branches is not None and zones is not None:
            if not branches.exists() and not zones.exists():
                raise forms.ValidationError('Select at least one branch or zone.')
            if any(not branch.is_active or not branch.zone.is_active for branch in branches) or any(not zone.is_active for zone in zones):
                raise forms.ValidationError('Remove paused branches or zones before saving this grant.')
        elif mode in {'ALL', 'NONE'} and branches is not None and zones is not None:
            if branches.exists() or zones.exists():
                raise forms.ValidationError('Clear branch and zone selections for All access or No access.')
        if mode == 'NONE':
            data['expires_at'] = None
        elif data.get('expires_at') and data['expires_at'] <= timezone.now():
            self.add_error('expires_at', 'Choose a future expiry time, or leave it empty.')
        return data


def policy_state(policy):
    if not policy or not policy.pk:
        return {'mode': 'NONE'}
    return {'user_id': policy.user_id, 'username': policy.user.username, 'mode': policy.mode,
            'branches': list(policy.branches.order_by('code').values_list('code', flat=True)),
            'zones': list(policy.zones.order_by('code').values_list('code', flat=True)),
            'expires_at': policy.expires_at.isoformat() if policy.expires_at else None}


def record_event(actor, subject, action, reason, before, after):
    # Required part of the same transaction: an audit failure rolls back grants.
    LoanAccessEvent.objects.create(actor=actor, subject=subject, action=action,
                                  actor_label=actor.username, subject_label=subject.username if subject else '',
                                  reason=reason, before=before, after=after)


@staff_member_required(login_url='/login/')
@permission_required('agentic_ai.manage_loan_access', raise_exception=True)
@csrf_protect
def access_manager(request):
    if request.method not in {'GET', 'POST'}:
        return HttpResponseNotAllowed(['GET', 'POST'])
    user_id = (request.POST.get('subject_id') or request.POST.get('user')) if request.method == 'POST' else request.GET.get('user')
    if user_id and (not user_id.isdecimal() or len(user_id) > 18):
        raise Http404
    instance = LoanAccessPolicy.objects.filter(user_id=user_id).first() if user_id else None
    with timezone.override('Asia/Dhaka'):
        form = AccessForm(request.POST if request.method == 'POST' else None, instance=instance,
                          initial={'user': user_id} if user_id else None, actor=request.user)
        if request.method == 'POST' and form.is_valid():
            subject = form.cleaned_data['user']
            try:
                with transaction.atomic():
                    # Lock the user as well so concurrent first grants serialize on PostgreSQL.
                    get_user_model().objects.select_for_update().get(pk=subject.pk)
                    current = LoanAccessPolicy.objects.select_for_update().filter(user=subject).first()
                    version = current.updated_at.isoformat() if current else 'new'
                    if version != form.cleaned_data['version']:
                        form.add_error(None, 'This policy changed in another session. Reload before saving.')
                    else:
                        before = policy_state(current)
                        policy = form.save(commit=False)
                        policy.updated_by = request.user
                        policy.save()
                        form.save_m2m()
                        record_event(request.user, subject, 'policy_saved', form.cleaned_data['reason'], before, policy_state(policy))
                        return redirect(f'{request.path}?user={subject.pk}&saved=1')
            except IntegrityError:
                form.add_error(None, 'The policy changed concurrently. Reload and try again.')
            except OperationalError as exc:
                if 'locked' not in str(exc).lower():
                    raise
                form.add_error(None, 'Another administrator is saving access. Reload and try again; this change was not saved.')
        users = get_user_model().objects.order_by('username').select_related('loan_access_policy')
        query = request.GET.get('q', '').strip()[:150]
        if query:
            users = users.filter(username__icontains=query)
        page = Paginator(users, 25).get_page(request.GET.get('page'))
        response = render(request, 'agentic_ai/data_access.html', {
            'form': form, 'users_page': page, 'query': query,
            'saved': request.method == 'GET' and request.GET.get('saved') == '1',
            'events': LoanAccessEvent.objects.select_related('actor', 'subject')[:15],
            'zone_count': LoanZone.objects.filter(is_active=True).count(),
            'now': timezone.now(),
            'branch_count': LoanBranch.objects.filter(is_active=True, zone__is_active=True).count(),
            'signing_ready': len(os.environ.get('LOAN_ACCESS_SIGNING_KEY', '').encode()) >= 32,
        })
    response['Cache-Control'] = 'no-store'
    return response


class ZoneForm(forms.ModelForm):
    reason = forms.CharField(min_length=8, max_length=500)
    class Meta:
        model = LoanZone
        fields = ('code', 'name', 'is_active')


class BranchForm(forms.ModelForm):
    reason = forms.CharField(min_length=8, max_length=500)
    class Meta:
        model = LoanBranch
        fields = ('code', 'name', 'zone', 'is_active')


def directory_state(record):
    return {'code': record.code, 'name': record.name, 'is_active': record.is_active,
            **({'zone': record.zone.code, 'area_code': record.area_code, 'area_name': record.area_name} if isinstance(record, LoanBranch) else {'division_code': record.division_code, 'division_name': record.division_name})}


def import_branches(upload, actor, reason):
    if not 8 <= len(reason) <= 500:
        raise ValueError('Provide a justification of 8–500 characters.')
    if upload.size > 2_000_000:
        raise ValueError('CSV must be at most 2 MB.')
    reader = csv.DictReader(io.StringIO(upload.read().decode('utf-8-sig')))
    if reader.fieldnames != ['zone_code', 'zone_name', 'branch_code', 'branch_name', 'active']:
        raise ValueError('Use the provided CSV template and column order.')
    rows = list(islice(reader, 5001))
    if not 1 <= len(rows) <= 5000:
        raise ValueError('Import between 1 and 5000 branches at a time.')
    seen = set()
    with transaction.atomic():
        for index, row in enumerate(rows, 2):
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f'Row {index}: invalid column count.')
            row = {key: value.strip() for key, value in row.items()}
            if row['branch_code'] in seen or row['active'] not in {'0', '1'}:
                raise ValueError(f'Row {index}: duplicate branch or invalid active value (use 0 or 1).')
            seen.add(row['branch_code'])
            zone = LoanZone.objects.filter(code=row['zone_code']).first()
            if zone and zone.name != row['zone_name']:
                raise ValueError(f'Row {index}: zone name differs from the directory. Update the zone explicitly first.')
            if not zone:
                zone = LoanZone(code=row['zone_code'], name=row['zone_name'])
                zone.full_clean()
                zone.save()
                record_event(actor, None, 'zone_created', reason, {}, directory_state(zone))
            branch = LoanBranch.objects.filter(code=row['branch_code']).first()
            before = directory_state(branch) if branch else {}
            branch = branch or LoanBranch(code=row['branch_code'])
            branch.name, branch.zone, branch.is_active = row['branch_name'], zone, row['active'] == '1'
            branch.full_clean()
            branch.save()
            record_event(actor, None, 'branch_imported', reason, before, directory_state(branch))
    return len(rows)


@staff_member_required(login_url='/login/')
@permission_required('agentic_ai.manage_loan_directory', raise_exception=True)
@csrf_protect
def directory_manager(request):
    if request.method not in {'GET', 'POST'}:
        return HttpResponseNotAllowed(['GET', 'POST'])
    if request.method == 'GET' and request.GET.get('template') == '1':
        response = HttpResponse('zone_code,zone_name,branch_code,branch_name,active\n', content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="branch-directory-template.csv"'
        return response
    from .directory_config import public_status
    zone_form, branch_form = ZoneForm(prefix='zone'), BranchForm(prefix='branch')
    notice = 'Directory changes saved and audited. Existing policies resolve against the updated directory.' if request.method == 'GET' and request.GET.get('saved') == '1' else ''
    error = ''
    sync_preview = None
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action in {'sync_preview', 'sync_apply'}:
                from .directory_sync import preview, apply_preview
                if action == 'sync_preview':
                    sync_preview = preview(request.user, request.POST.get('activate_new') == '1')
                else:
                    apply_preview(request.user, request.POST.get('ticket', ''), request.POST.get('reason', '').strip())
                    notice = 'Oracle directory synchronized and audited.'
            elif action == 'import':
                upload = request.FILES.get('file')
                if not upload:
                    raise ValueError('Choose a CSV file.')
                count = import_branches(upload, request.user, request.POST.get('reason', '').strip())
                notice = f'{count} branches imported. Existing policies resolve against the updated directory.'
            elif action in {'zone', 'branch'}:
                model, form_class = (LoanZone, ZoneForm) if action == 'zone' else (LoanBranch, BranchForm)
                # Exact code identifies an existing record; leading zeros are preserved.
                code = request.POST.get(f'{action}-code', '').strip()
                with transaction.atomic():
                    instance = model.objects.select_for_update().filter(code=code).first()
                    before = directory_state(instance) if instance else {}
                    form = form_class(request.POST, instance=instance, prefix=action)
                    if form.is_valid():
                        record = form.save()
                        record_event(request.user, None, f'{action}_saved', form.cleaned_data['reason'], before, directory_state(record))
                        notice = 'Directory saved. A scope change invalidates previously issued report access.'
                    elif action == 'zone':
                        zone_form = form
                    else:
                        branch_form = form
            else:
                raise ValueError('Choose a directory action.')
        except (ValueError, UnicodeError, ValidationError, csv.Error) as exc:
            error = str(exc)
        except (IntegrityError, OperationalError):
            error = 'The directory could not be saved. No changes from this request were applied. Reload and try again.'
        if notice and not error:
            return redirect(f'{request.path}?saved=1')
    response = render(request, 'agentic_ai/access_directory.html', {
        'zone_form': zone_form, 'branch_form': branch_form, 'notice': notice, 'error': error,
        'sync_preview': sync_preview, 'directory_configured': bool(public_status()['url']),
        'zones': LoanZone.objects.all(),
        'branches_page': Paginator(LoanBranch.objects.select_related('zone').all(), 50).get_page(request.GET.get('page')),
    })
    response['Cache-Control'] = 'no-store'
    return response
