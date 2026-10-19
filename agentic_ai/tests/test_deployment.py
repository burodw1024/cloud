import os
import runpy
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.test import SimpleTestCase, override_settings

from agentic_ai.checks import check_agent_deployment
from agentic_ai.models import AgentTool


class DeploymentTests(SimpleTestCase):
    def test_production_requires_environment(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ImproperlyConfigured):
                runpy.run_module("buro_first.settings_production")

    def test_production_profile_has_shared_cache_secure_cookies_and_no_debug(self):
        env = {"DJANGO_SECRET_KEY": "a" * 64, "DJANGO_ALLOWED_HOSTS": "agent.example.com"}
        with patch.dict(os.environ, env, clear=True):
            config = runpy.run_module("buro_first.settings_production")
        self.assertFalse(config["DEBUG"])
        self.assertTrue(config["CSRF_COOKIE_SECURE"])
        self.assertTrue(config["SESSION_COOKIE_SECURE"])
        self.assertEqual(config["DATABASES"]["default"]["ENGINE"], "django.db.backends.sqlite3")
        self.assertTrue(config["CACHES"]["default"]["BACKEND"].endswith("SQLiteCache"))

    def test_invalid_role_configuration_rejected(self):
        for roles in ("manager", {"role": "manager"}, [123], [" manager"], [""]):
            with self.assertRaises(ValidationError):
                AgentTool(name="loan_aging", allowed_roles=roles).clean()

    @override_settings(DEBUG=True, CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
    def test_deploy_check_rejects_debug_and_local_cache(self):
        with patch("agentic_ai.mcp_gateway.gateway_enabled", return_value=True):
            messages = check_agent_deployment(None)
        self.assertTrue({"agentic_ai.E001", "agentic_ai.E002"}.issubset({m.id for m in messages}))
