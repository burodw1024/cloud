"""Diagnose MCP discovery independently of Ollama and the ContextForge UI."""
import asyncio
import os
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from jsonschema import validate


async def probe(test_read=False):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    url = os.getenv('AGENT_MCP_URL', '')
    token = os.getenv('AGENT_MCP_TOKEN', '')
    if not url or not token:
        raise ValueError('MCP configuration missing')
    async with streamablehttp_client(
        url, headers={'Authorization': 'Bearer ' + token},
        timeout=timedelta(seconds=20), sse_read_timeout=timedelta(seconds=30),
    ) as (read, write, _):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=30)) as session:
            await session.initialize()
            tools = []
            cursor = None
            for _ in range(10):
                page = await session.list_tools(cursor=cursor)
                tools.extend(page.tools)
                cursor = page.nextCursor
                if not cursor:
                    break
            else:
                raise ValueError('Catalog limit exceeded')
            names = [tool.name for tool in tools]
            if test_read:
                tool = next((tool for tool in tools if tool.name == 'nifi-db-read'), None)
                if tool is None or getattr(tool.annotations, 'destructiveHint', False):
                    raise ValueError('Read test tool unavailable or marked destructive')
                arguments = {'sql': 'SELECT 1 AS MCP_TEST FROM DUAL'}
                validate(arguments, tool.inputSchema)
                result = await session.call_tool(tool.name, arguments=arguments)
                if result.isError:
                    raise RuntimeError('Read test returned MCP error')
                # This fixed query returns a constant, not business records.
                text = '\n'.join(getattr(block, 'text', '') for block in result.content)
                structured = str(result.structuredContent or '')
                if 'MCP_TEST' not in text + structured or '1' not in text + structured:
                    raise ValueError('Read test did not return expected marker')
            return names


class Command(BaseCommand):
    help = 'Verify ContextForge MCP discovery; optionally test the fixed Oracle SELECT 1 query.'

    def add_arguments(self, parser):
        parser.add_argument('--test-read', action='store_true', help='Execute SELECT 1 AS MCP_TEST FROM DUAL only')

    def handle(self, *args, **options):
        if not os.getenv('AGENT_MCP_URL') or not os.getenv('AGENT_MCP_TOKEN'):
            raise CommandError('Set AGENT_MCP_URL and AGENT_MCP_TOKEN securely before running this check.')
        async def bounded():
            return await asyncio.wait_for(probe(options['test_read']), timeout=60)
        try:
            names = asyncio.run(bounded())
        except Exception as exc:
            raise CommandError('MCP check failed (' + type(exc).__name__ + '). Check gateway logs; no credentials or tool data printed.') from None
        self.stdout.write('MCP initialization and discovery passed. Tools: ' + ', '.join(names))
        if options['test_read']:
            self.stdout.write('Fixed Oracle read test passed: MCP_TEST=1')
