from django.core.management.base import BaseCommand, CommandError
from chat.dynamic_config import get_setting
from agentic_ai.mcp_client import MCPClient, MCPError
from agentic_ai.models import AgentTool


class Command(BaseCommand):
    help = "Discover ContextForge MCP tools. Optionally register new tools disabled and requiring approval."

    def add_arguments(self, parser):
        parser.add_argument('--register', action='store_true')

    def handle(self, *args, **options):
        url = get_setting('MCP_SERVER_URL', '')
        if not url:
            raise CommandError('Set MCP_SERVER_URL to the virtual server Streamable HTTP connection URL.')
        try:
            with MCPClient(url, get_setting('MCP_BEARER_TOKEN', '')) as client:
                for tool in client.list_tools():
                    name = tool.get('name')
                    if not isinstance(name, str) or not name or len(name) > 150:
                        self.stderr.write('Skipped a tool with an invalid or oversized name.')
                        continue
                    # Only print tool names, not tokens or tool result contents.
                    self.stdout.write(name)
                    if options['register']:
                        AgentTool.objects.get_or_create(name=name, defaults={
                            'description': str(tool.get('description', ''))[:4000],
                            'is_active': False, 'requires_approval': True, 'allowed_roles': [],
                        })
        except MCPError as exc:
            raise CommandError(str(exc)) from exc
        if options['register']:
            self.stdout.write('New tools are disabled. Review roles and approval rules in Django admin before enabling them.')
