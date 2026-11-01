"""Business configuration, separate from gateway credentials and model settings."""
from django import forms
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import permission_required
from django.db import transaction
from django.db.models import Q, Count
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_protect
from django.utils.text import slugify

from .models import BusinessModule, AgentTool


MODULE_PERMISSIONS = ('agentic_ai.add_businessmodule', 'agentic_ai.change_businessmodule',
                      'agentic_ai.change_agenttool')


class ToolChoices(forms.ModelMultipleChoiceField):
    def label_from_instance(self, tool):
        return f'{tool.display_name or tool.name} — {tool.name}'


class ModuleForm(forms.ModelForm):
    tools = ToolChoices(queryset=AgentTool.objects.none(), required=False,
                        widget=forms.CheckboxSelectMultiple)

    class Meta:
        model = BusinessModule
        fields = ('name', 'slug', 'description', 'starter_questions', 'sort_order', 'is_active')
        widgets = {'description': forms.Textarea(attrs={'rows': 3}),
                   'starter_questions': forms.Textarea(attrs={'rows': 5})}
        labels = {'name': 'Module name', 'slug': 'URL name', 'is_active': 'Published',
                  'starter_questions': 'Starter questions · one per line', 'sort_order': 'Navigation order'}
        help_texts = {'slug': 'A stable URL name, for example loan-reports or hr-services.',
                      'starter_questions': 'Use examples supported by the assigned tools. Clicking an example only prepares a question.',
                      'is_active': 'Uncheck to pause this module and block its MCP tools. Publishing does not enable tools or grant roles.'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['slug'].required = False
        self.fields['tools'].queryset = AgentTool.objects.filter(
            Q(module__isnull=True) | Q(module=self.instance.pk) if self.instance.pk else Q(module__isnull=True)
        ).order_by('display_name', 'name')
        if self.instance.pk:
            self.initial['tools'] = self.instance.tools.values_list('pk', flat=True)
        else:
            self.initial['is_active'] = False

    def clean_slug(self):
        value = self.cleaned_data.get('slug') or slugify(self.cleaned_data.get('name', ''), allow_unicode=True)
        if not value:
            raise forms.ValidationError('Enter a URL name for this module.')
        return value

    def clean_starter_questions(self):
        questions = [line.strip() for line in self.cleaned_data.get('starter_questions', '').splitlines() if line.strip()]
        if len(questions) > 20 or any(len(question) > 500 for question in questions):
            raise forms.ValidationError('Use up to 20 questions, each at most 500 characters.')
        return '\n'.join(dict.fromkeys(questions))


@staff_member_required(login_url='/login/')
@permission_required(MODULE_PERMISSIONS, raise_exception=True)
@csrf_protect
def module_manager(request):
    if request.method not in {'GET', 'POST'}:
        return HttpResponseNotAllowed(['GET', 'POST'])
    module_id = request.POST.get('module_id') if request.method == 'POST' else request.GET.get('edit')
    # Avoid unhandled identifier errors while keeping object lookup permission protected.
    instance = get_object_or_404(BusinessModule, pk=module_id) if module_id and module_id.isdecimal() else None
    if module_id and instance is None:
        from django.http import Http404
        raise Http404
    form = ModuleForm(request.POST if request.method == 'POST' else None, instance=instance)
    if request.method == 'POST' and form.is_valid():
        with transaction.atomic():
            selected = list(form.cleaned_data['tools'].values_list('pk', flat=True))
            # Lock and recheck assignment so simultaneous edits cannot steal another module's tools.
            locked = list(AgentTool.objects.select_for_update().filter(pk__in=selected))
            if any(tool.module_id not in {None, instance.pk if instance else None} for tool in locked):
                form.add_error('tools', 'A tool was assigned elsewhere. Refresh and try again.')
            else:
                module = form.save()
                AgentTool.objects.filter(module=module).exclude(pk__in=selected).update(module=None)
                AgentTool.objects.filter(pk__in=selected).update(module=module)
                # Post/redirect/get keeps refresh from submitting the form twice.
                return redirect(f'{request.path}?edit={module.pk}&saved=1')
    modules = BusinessModule.objects.annotate(tool_count=Count('tools'))
    response = render(request, 'agentic_ai/modules.html', {'modules': modules, 'form': form,
                      'editing': instance, 'saved': request.GET.get('saved') == '1'})
    response['Cache-Control'] = 'no-store'
    return response
