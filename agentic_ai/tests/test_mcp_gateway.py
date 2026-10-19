import json
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from asgiref.sync import async_to_sync
from django.contrib.auth.models import Group, User
from django.test import TestCase, SimpleTestCase
from jsonschema import ValidationError
from mcp.types import Tool

from agentic_ai import mcp_gateway as gateway
from agentic_ai.models import AgentTool, AgentApprovalRequest, AgentRunLog

SCHEMA = {"type": "object", "properties": {
    "reportDate": {"type": "string", "format": "date"},
    "branchCode": {"type": "string"}}, "additionalProperties": False}
TOOL = Tool(name="loans/aging.report", description="Loan aging by branch and date", inputSchema=SCHEMA)
ARGS = {"branchCode": "0253", "reportDate": "2026-09-30"}


def message(arguments=ARGS, name="mcp_tool_0"):
    return {"tool_calls": [{"function": {"name": name, "arguments": json.dumps(arguments)}}]}


class SelectionTests(SimpleTestCase):
    def validate(self, msg):
        return gateway._validate_call(msg, {"mcp_tool_0": TOOL.name}, {"mcp_tool_0": SCHEMA})

    def test_preserves_branch_leading_zero_and_date(self):
        self.assertEqual(self.validate(message()), (TOOL.name, ARGS))

    def test_unknown_tool_is_rejected(self):
        with self.assertRaises(gateway.GatewayError):
            self.validate(message(name="unregistered"))

    def test_extra_argument_is_rejected(self):
        with self.assertRaises(gateway.GatewayError):
            self.validate(message(dict(ARGS, sql="delete")))

    def test_wrong_branch_type_is_rejected(self):
        with self.assertRaises(gateway.GatewayError):
            self.validate(message(dict(ARGS, branchCode=253)))

    def test_invalid_date_is_rejected(self):
        with self.assertRaises(gateway.GatewayError):
            self.validate(message(dict(ARGS, reportDate="2026-02-30")))

    def test_multiple_calls_rejected(self):
        msg = message()
        msg["tool_calls"] *= 2
        with self.assertRaises(gateway.GatewayError):
            self.validate(msg)

    def test_no_call_allows_clarification(self):
        self.assertIsNone(self.validate({"content": "Which branch?"}))

    def test_both_model_protocols(self):
        real_client = httpx.AsyncClient
        for provider in ("ollama", "openai"):
            requests = []
            def respond(request):
                requests.append(request)
                body = {"message": message()} if provider == "ollama" else {"choices": [{"message": message()}]}
                return httpx.Response(200, json=body)
            def client(**kwargs):
                return real_client(transport=httpx.MockTransport(respond), **kwargs)
            config = {"provider": provider, "model": "configured-model", "model_url": "https://model.example/v1",
                      "model_key": "", "timeout": 1}
            with patch.object(gateway.httpx, "AsyncClient", side_effect=client):
                _, selection = async_to_sync(gateway._select_tool)(config, "loan aging", [TOOL])
            self.assertEqual(selection, (TOOL.name, ARGS))
            payload = json.loads(requests[0].content)
            self.assertEqual(payload["tools"][0]["function"]["parameters"], SCHEMA)
            self.assertTrue(str(requests[0].url).endswith("/api/chat" if provider == "ollama" else "/chat/completions"))


class RoleTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("manager-user")
        self.user.groups.add(Group.objects.create(name="manager"))
        self.record = AgentTool.objects.create(name=TOOL.name, allowed_roles=["manager"])

    def test_only_active_matching_registered_tools_are_available(self):
        AgentTool.objects.create(name="inactive", allowed_roles=["manager"], is_active=False)
        AgentTool.objects.create(name="restricted", allowed_roles=["cashflow"])
        AgentTool.objects.create(name="empty", allowed_roles=[])
        self.assertEqual(gateway._authorized_names(self.user), {TOOL.name})

    def test_no_tools_does_not_connect(self):
        self.record.delete()
        with patch.object(gateway, "_run") as run:
            result = gateway.run_mcp_query(self.user, "loan aging")
        run.assert_not_called()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(AgentRunLog.objects.get().status, "blocked")

    def test_revocation_rechecked_before_execution(self):
        self.record.is_active = False
        self.record.save()
        self.assertEqual(gateway._decision(self.user, TOOL.name, ARGS)["status"], "blocked")

    def test_approval_records_exact_arguments(self):
        self.record.requires_approval = True
        self.record.save()
        result = gateway._decision(self.user, TOOL.name, ARGS)
        self.assertTrue(result["approval_required"])
        self.assertEqual(AgentApprovalRequest.objects.get().request_payload["arguments"], ARGS)

    def test_existing_project_model_and_url_are_defaults(self):
        values = {"MCP_GATEWAY_URL": "https://gateway.example/mcp",
                  "OLLAMA_URL": "http://project-model.example:11434",
                  "MCP_MODEL": "", "MCP_MODEL_BASE_URL": ""}
        run = AsyncMock(return_value={"ok": True, "answer": "Which report date?"})
        with patch.object(gateway, "get_setting", side_effect=lambda key, default="": values.get(key, default)), \
             patch.object(gateway, "get_llm_model", return_value="project-selected-model"), \
             patch.object(gateway, "_run", new=run):
            result = gateway.run_mcp_query(self.user, "loan aging")
        self.assertTrue(result["ok"])
        config = run.call_args.args[0]
        self.assertEqual(config["model"], "project-selected-model")
        self.assertEqual(config["model_url"], values["OLLAMA_URL"])
        self.assertEqual(config["url"], values["MCP_GATEWAY_URL"])

    def test_admin_still_needs_active_registration_with_roles(self):
        self.user.is_superuser = True
        self.record.allowed_roles = []
        self.record.save()
        self.assertEqual(gateway._authorized_names(self.user), set())


class GatewayExecutionTests(SimpleTestCase):
    def setUp(self):
        self.config = {"url": "https://gateway.example/mcp", "token": "", "timeout": 1}

    def run_gateway(self, tools, selection, decision=None, error=False):
        from contextlib import asynccontextmanager
        from mcp.types import CallToolResult, TextContent

        @asynccontextmanager
        async def transport(*args, **kwargs):
            yield (None, None, None)

        session = AsyncMock()
        session.__aenter__.return_value = session
        session.list_tools.return_value = SimpleNamespace(tools=tools, nextCursor=None)
        session.call_tool.return_value = CallToolResult(
            content=[TextContent(type="text", text="Loan report result")], isError=error)
        with patch("mcp.client.streamable_http.streamablehttp_client", transport), \
             patch("mcp.ClientSession", return_value=session), \
             patch.object(gateway, "_select_tool", new=AsyncMock(return_value=({}, selection))) as select, \
             patch.object(gateway, "_decision", new=lambda *args: decision):
            async def invoke():
                return await asyncio.wait_for(gateway._run(self.config, None, "loan aging", {TOOL.name}), timeout=3)
            result = async_to_sync(invoke)()
        return result, session, select

    def test_discovery_filters_unregistered_tools(self):
        other = Tool(name="delete_all", inputSchema={"type": "object"})
        result, session, select = self.run_gateway([TOOL, other], (TOOL.name, ARGS))
        self.assertTrue(result["ok"])
        self.assertEqual(select.call_args.args[2], [TOOL])
        session.call_tool.assert_awaited_once_with(TOOL.name, arguments=ARGS)

    def test_no_matching_gateway_tool_does_not_call_model(self):
        result, session, select = self.run_gateway([], None)
        self.assertEqual(result["status"], "blocked")
        select.assert_not_awaited()
        session.call_tool.assert_not_awaited()

    def test_approval_blocks_execution(self):
        result, session, _ = self.run_gateway([TOOL], (TOOL.name, ARGS),
                                             {"ok": False, "approval_required": True})
        self.assertTrue(result["approval_required"])
        session.call_tool.assert_not_awaited()

    def test_mcp_tool_error_is_not_success(self):
        result, _, _ = self.run_gateway([TOOL], (TOOL.name, ARGS), error=True)
        self.assertFalse(result["ok"])


class HardeningTests(SimpleTestCase):
    def test_external_reference_rejected_before_resolution(self):
        for key in ("$ref", "$dynamicRef", "$recursiveRef"):
            with self.assertRaises(gateway.GatewayError):
                gateway._validator({key: "http://untrusted.example/schema"})

    def test_internal_reference_supported(self):
        validator = gateway._validator({"type": "object", "$defs": {"code": {"type": "string"}},
                                        "properties": {"branchCode": {"$ref": "#/$defs/code"}}})
        validator.validate({"branchCode": "0253"})

    def test_gateway_configuration_rejects_bad_urls_and_timeouts(self):
        for url in ("file:///etc/passwd", "https://user:secret@host/mcp", "https://host/mcp?token=secret", "http://host:invalid/mcp"):
            with self.assertRaises(gateway.GatewayError):
                gateway._http_url(url, "gateway")
        for value in ("0", "-1", "nan", "inf", "bad", "1000"):
            with patch.object(gateway, "get_setting", return_value=value):
                with self.assertRaises(gateway.GatewayError):
                    gateway._number_setting("timeout", 30, 1, 300)

    def test_discovery_duplicate_and_cursor_limits(self):
        session = AsyncMock()
        session.list_tools.return_value = SimpleNamespace(tools=[TOOL, TOOL], nextCursor=None)
        with self.assertRaises(gateway.GatewayError):
            async_to_sync(gateway.discover_tools)(session)
        session.list_tools.return_value = SimpleNamespace(tools=[], nextCursor="same")
        with self.assertRaises(gateway.GatewayError):
            async_to_sync(gateway.discover_tools)(session)

    def test_catalog_size_limit(self):
        session = AsyncMock()
        tool = Tool(name="large", description="x" * (gateway.MAX_CATALOG_BYTES + 1), inputSchema=SCHEMA)
        session.list_tools.return_value = SimpleNamespace(tools=[tool], nextCursor=None)
        with self.assertRaises(gateway.GatewayError):
            async_to_sync(gateway.discover_tools)(session)

    def test_overall_deadline(self):
        async def stalled(*args):
            await asyncio.sleep(10)
        with patch.object(gateway, "_run", new=stalled):
            with self.assertRaises(asyncio.TimeoutError):
                async_to_sync(gateway._bounded_run)({"request_timeout": .01}, None, "loan", set())

    def test_upstream_error_body_not_exposed(self):
        request = httpx.Request("POST", "https://host/mcp")
        response = httpx.Response(403, text="sensitive upstream content", request=request)
        code, answer, status = gateway._failure(httpx.HTTPStatusError("secret", request=request, response=response), {})
        self.assertEqual(code, "upstream_authentication")
        self.assertNotIn("secret", answer)
        self.assertEqual(status, 502)


class AuditHardeningTests(TestCase):
    setUp = RoleTests.setUp

    def test_audit_failure_prevents_network(self):
        with patch.object(AgentRunLog.objects, "create", side_effect=RuntimeError), patch.object(gateway, "_run") as run:
            result = gateway.run_mcp_query(self.user, "loan aging")
        self.assertEqual(result["code"], "audit_unavailable")
        run.assert_not_called()

    def test_dispatched_timeout_reports_uncertain_outcome(self):
        async def timeout(config, *args):
            config["state"].update(dispatched=True, tool=TOOL.name)
            raise asyncio.TimeoutError
        config = {"request_timeout": 1}
        with patch.object(gateway, "load_config", return_value=config), patch.object(gateway, "_run", new=timeout):
            result = gateway.run_mcp_query(self.user, "loan aging")
        self.assertTrue(result["outcome_unknown"])
        self.assertEqual(result["code"], "upstream_timeout")
        self.assertEqual(AgentRunLog.objects.get().selected_tool, TOOL.name)


class ProjectModelFallbackTests(SimpleTestCase):
    def test_ollama_unsupported_native_tools_uses_validated_json_plan(self):
        real_client = httpx.AsyncClient
        requests = []
        def respond(request):
            body = json.loads(request.content)
            requests.append(body)
            if body.get("tools"):
                return httpx.Response(400, json={"error": "project-model does not support tools"})
            return httpx.Response(200, json={"message": {"content": json.dumps({
                "tool": "mcp_tool_0", "arguments": ARGS, "clarification": ""})}})
        def client(**kwargs):
            return real_client(transport=httpx.MockTransport(respond), **kwargs)
        config = {"provider": "ollama", "model": "existing-project-model", "model_url": "https://model.example",
                  "model_key": "", "timeout": 1}
        with patch.object(gateway.httpx, "AsyncClient", side_effect=client):
            msg, selection = async_to_sync(gateway._select_tool)(config, "loan aging", [TOOL])
        self.assertEqual(selection, (TOOL.name, ARGS))
        self.assertEqual(msg["selection_mode"], "structured_json")
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0]["model"], requests[1]["model"])
        self.assertNotIn("tools", requests[1])
        self.assertIsInstance(requests[1]["format"], dict)

    def test_arbitrary_model_rejection_does_not_use_fallback(self):
        with patch.object(gateway, "_model_response", new=AsyncMock(side_effect=gateway.GatewayError("model_configuration_error", "bad model"))) as response:
            with self.assertRaises(gateway.GatewayError):
                async_to_sync(gateway._select_tool)({"provider": "ollama", "model": "x"}, "loan", [TOOL])
        self.assertEqual(response.await_count, 1)
