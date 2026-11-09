from django.db import migrations


def configure_loan_due(apps, schema_editor):
    # The operator has installed QueryLoanDueAppScope. Configure the tool,
    # never user grants: UserToolAccess / LoanAccessPolicy remain authoritative.
    apps.get_model('agentic_ai', 'AgentTool').objects.using(schema_editor.connection.alias).filter(
        name='loan-due-report',
    ).update(loan_access_mode='APP', requires_loan_scope=True, fixed_branch_codes=[])


class Migration(migrations.Migration):
    dependencies = [('agentic_ai', '0010_alter_agenttool_loan_access_mode')]
    operations = [migrations.RunPython(configure_loan_due, migrations.RunPython.noop)]
