from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError
from django.test import TestCase, RequestFactory

from agentic_ai.models import AgentRunLog


class AuditStorageTests(TestCase):
    def test_probe_leaves_existing_logs_unchanged(self):
        audit = AgentRunLog.objects.create(user_query="Existing record")
        call_command("check_audit_storage", stdout=StringIO())
        self.assertEqual(list(AgentRunLog.objects.values_list("pk", flat=True)), [audit.pk])

    def test_probe_fails_without_exposing_database_error(self):
        with patch.object(AgentRunLog.objects, "create", side_effect=OperationalError("private details")):
            with self.assertRaises(CommandError) as error:
                call_command("check_audit_storage", stdout=StringIO())
        self.assertIn("OperationalError", str(error.exception))
        self.assertNotIn("private details", str(error.exception))

    def test_readiness_rejects_missing_audit_column(self):
        from buro_first.health import ready

        with patch.object(AgentRunLog.objects, "values", side_effect=OperationalError("missing column")):
            response = ready(RequestFactory().get("/healthz/ready/"))
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(b"missing column", response.content)

    def test_readiness_accepts_migrated_empty_audit_table(self):
        from buro_first.health import ready

        response = ready(RequestFactory().get("/healthz/ready/"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(AgentRunLog.objects.count(), 0)
