from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class WorkspaceMigrationTests(TransactionTestCase):
    def test_existing_tool_roles_activation_and_approval_survive_metadata_migration(self):
        target=('agentic_ai', '0002_agenttool_category_agenttool_display_name_and_more')
        executor=MigrationExecutor(connection)
        try:
            executor.migrate([('agentic_ai','0001_initial')])
            old=executor.loader.project_state([('agentic_ai','0001_initial')]).apps
            Tool=old.get_model('agentic_ai','AgentTool')
            record=Tool.objects.create(name='existing-service', description='Existing description',
                allowed_roles=['manager','staff'], is_active=False, requires_approval=True)
            old_log=old.get_model('agentic_ai','AgentRunLog').objects.create(user_query='Existing request', final_answer='Existing answer', status='success')
            executor=MigrationExecutor(connection)
            executor.migrate([target])
            new=executor.loader.project_state([target]).apps.get_model('agentic_ai','AgentTool')
            upgraded=new.objects.get(pk=record.pk)
            self.assertEqual(upgraded.allowed_roles,['manager','staff'])
            self.assertFalse(upgraded.is_active)
            self.assertTrue(upgraded.requires_approval)
            self.assertEqual(upgraded.description,'Existing description')
            self.assertEqual(upgraded.category,'General')
            self.assertEqual(upgraded.display_name,'')
            self.assertEqual(upgraded.example_question,'')
            usage_target=('agentic_ai','0003_agentrunlog_usage_summary')
            executor=MigrationExecutor(connection)
            executor.migrate([usage_target])
            Log=executor.loader.project_state([usage_target]).apps.get_model('agentic_ai','AgentRunLog')
            upgraded_log=Log.objects.get(pk=old_log.pk)
            self.assertEqual(upgraded_log.final_answer,'Existing answer')
            self.assertEqual(upgraded_log.usage_summary,{})
        finally:
            restore = MigrationExecutor(connection)
            restore.migrate(restore.loader.graph.leaf_nodes())
