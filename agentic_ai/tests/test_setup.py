import json
from unittest.mock import AsyncMock, patch

from django.contrib.auth.models import Group, User
from django.core.cache import cache
from django.test import TestCase

from agentic_ai import setup_views
from agentic_ai.models import AgentTool
from .test_mcp_gateway import TOOL

CONFIG = {"url": "https://gateway.example/mcp", "provider": "ollama", "model": "project-model",
          "model_url": "https://model.example", "model_key": ""}


class GuidedSetupTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("setup-admin", is_staff=True, is_superuser=True)
        Group.objects.create(name="manager")
        self.client.force_login(self.user)
        self.url = "/agentic/setup/"
        self.patches = [
            patch.object(setup_views, "load_config", return_value=CONFIG),
            patch.object(setup_views, "load_gateway_config", return_value=CONFIG),
            patch.object(setup_views, "discover_gateway", new=AsyncMock(return_value=[TOOL])),
            patch.object(setup_views, "probe_model", new=AsyncMock(return_value={"model": "project-model"})),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def post(self, action, **kwargs):
        return self.client.post(self.url, json.dumps(dict(action=action, **kwargs)), content_type="application/json")

    def tool(self, **kwargs):
        return dict(name=TOOL.name, roles=["manager"], enabled=True, requires_approval=False, **kwargs)

    def test_main_page_has_universal_workspace_and_csrf(self):
        response = self.client.get("/agentic/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Your organization. Connected.")
        self.assertContains(response, "workspace-capabilities")
        self.assertNotContains(response, "Show the loan aging report for branch 0253")
        self.assertContains(response, "csrfmiddlewaretoken")
        self.assertContains(response, "Set up tools")

    def test_setup_requires_privileged_staff(self):
        self.user.is_superuser = False
        self.user.save()
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_discovery_does_not_auto_grant_access(self):
        response = self.post("discover")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(AgentTool.objects.exists())
        self.assertIn("manager", response.json()["roles"])

    @patch.object(setup_views, "_save_settings")
    def test_guided_flow_saves_exact_names_roles_and_enables_gateway(self, save):
        self.post("discover")
        self.post("check_model")
        response = self.post("save_tools", tools=[self.tool()])
        self.assertEqual(response.status_code, 200)
        record = AgentTool.objects.get()
        self.assertEqual(record.name, TOOL.name)
        self.assertEqual(record.allowed_roles, ["manager"])
        self.assertTrue(record.is_active)
        save.assert_called_once_with({"MCP_GATEWAY_ENABLED": "true"})

    def test_model_check_required_before_enable(self):
        self.post("discover")
        response = self.post("save_tools", tools=[self.tool()])
        self.assertEqual(response.status_code, 409)
        self.assertFalse(AgentTool.objects.exists())

    def test_arbitrary_tool_or_role_cannot_be_registered(self):
        self.post("discover")
        self.post("check_model")
        for item in (dict(self.tool(), name="unlisted"), dict(self.tool(), roles=["invented-role"])):
            self.assertEqual(self.post("save_tools", tools=[item]).status_code, 400)
        self.assertFalse(AgentTool.objects.exists())

    @patch.object(setup_views, "_save_settings")
    def test_blank_token_keeps_saved_secret(self, save):
        response = self.post("save_connection", url="https://gateway.example/mcp", token="")
        self.assertEqual(response.status_code, 200)
        save.assert_called_once_with({"MCP_GATEWAY_URL": "https://gateway.example/mcp", "MCP_GATEWAY_ENABLED": "false"})

    @patch.object(setup_views, '_save_settings')
    @patch.object(setup_views, 'get_setting', return_value='http://gateway:4444/servers/x/mcp')
    def test_summary_settings_keep_gateway_token_on_same_origin(self, setting, save):
        response = self.post('save_summary', base='http://gateway:4444/v1', model='openrouter/auto')
        self.assertEqual(response.status_code, 200)
        save.assert_called_once_with({'MCP_SUMMARY_BASE_URL': 'http://gateway:4444/v1', 'MCP_SUMMARY_MODEL': 'openrouter/auto'})
        self.assertEqual(self.post('save_summary', base='https://other.example/v1', model='openrouter/auto').status_code, 400)
