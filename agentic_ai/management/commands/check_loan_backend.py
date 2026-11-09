"""Inspect scoped MCP contracts without executing a financial report."""
import json
import os

from asgiref.sync import async_to_sync
from django.core.management.base import BaseCommand, CommandError

from agentic_ai.access_readiness import report_readiness
from agentic_ai.loan_access import protected_tools
from agentic_ai.mcp_gateway import discover_gateway, gateway_enabled, load_gateway_config
from agentic_ai.models import AgentTool


class Command(BaseCommand):
    help = 'Check financial MCP input schemas and local signing configuration; no report is executed.'

    def add_arguments(self, parser):
        parser.add_argument('--tool', help='Exact registered MCP tool name')

    def handle(self, *args, **options):
        if not gateway_enabled():
            raise CommandError('MCP gateway is disabled.')
        records = AgentTool.objects.select_related('module').order_by('name')
        if options['tool']:
            records = records.filter(name=options['tool'])
            if not records.exists():
                raise CommandError('Requested tool is not registered.')
        try:
            config = load_gateway_config()
            config.update(timeout=min(config['timeout'], 15), request_timeout=25)
            tools = async_to_sync(discover_gateway)(config)
        except Exception:
            raise CommandError('MCP discovery failed. Inspect Integration settings; credentials were not printed.') from None
        results = report_readiness(records, tools,
            len(os.environ.get('LOAN_ACCESS_SIGNING_KEY', '').encode()) >= 32, protected_tools())
        self.stdout.write(json.dumps({'report_executed': False, 'tools': results}, ensure_ascii=False, indent=2))
        if not results:
            raise CommandError('No registered financial tools matched.')
        if any(item['code'] not in {'schema_detected', 'application_scope_detected'} for item in results):
            raise CommandError('Per-user backend setup is incomplete; see the tool status above.')
        self.stdout.write('Input contracts detected. Verify backend query scope and trusted endpoint access in staging before activation.')
