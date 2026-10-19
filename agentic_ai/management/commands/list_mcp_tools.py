"""Check the gateway and project model without executing a gateway tool."""
import json
from asgiref.sync import async_to_sync
from django.core.management.base import BaseCommand, CommandError
from agentic_ai.mcp_gateway import discover_gateway, load_gateway_config, load_config, probe_model


class Command(BaseCommand):
    help = "List exact MCP tools. --check-model also probes project-model selection without running a tool."

    def add_arguments(self, parser):
        parser.add_argument("--check-model", action="store_true")

    def handle(self, *args, **options):
        try:
            tools = async_to_sync(discover_gateway)(load_gateway_config())
            output = {"tools": [{"name": t.name, "description": t.description,
                                 "inputSchema": t.inputSchema} for t in tools]}
            if options["check_model"]:
                output["model_check"] = async_to_sync(probe_model)(load_config())
            self.stdout.write(json.dumps(output, indent=2, ensure_ascii=False))
        except Exception:
            raise CommandError("MCP readiness failed. Verify endpoint, network access, credentials, tool catalog and the project model's native tool-calling support.") from None
