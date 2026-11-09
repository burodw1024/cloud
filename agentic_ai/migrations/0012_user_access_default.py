from django.db import migrations, models


def configure_user_access(apps, schema_editor):
    Tool = apps.get_model('agentic_ai', 'AgentTool')
    tools = Tool.objects.using(schema_editor.connection.alias)
    # Tool/branch grants, group restrictions, approvals, activation and module
    # publication stay unchanged. A financial tool still needs a scoped backend.
    tools.filter(loan_access_mode='FIXED').update(requires_loan_scope=True)
    tools.filter(loan_access_mode__in=['SIGNED', 'FIXED']).update(
        loan_access_mode='UI', fixed_branch_codes=[])


class Migration(migrations.Migration):
    dependencies = [('agentic_ai', '0011_loan_due_application_scope')]
    operations = [
        migrations.AlterField(model_name='agenttool', name='loan_access_mode',
            field=models.CharField(max_length=10, default='UI', choices=[
                ('UI', 'User Access permissions'), ('APP', 'Application-managed branch scope'),
                ('SIGNED', 'Legacy signed access'), ('FIXED', 'Legacy fixed NiFi scope')])),
        migrations.RunPython(configure_user_access, migrations.RunPython.noop),
    ]
