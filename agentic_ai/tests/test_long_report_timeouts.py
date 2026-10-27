"""Regression checks for the 1200s NiFi -> gateway -> Django Kubernetes path."""
import ast
import hashlib
from pathlib import Path
from unittest.mock import patch
from django.test import SimpleTestCase
from agentic_ai import mcp_gateway as gateway
from agentic_ai.api import IDEMPOTENCY_TTL

ROOT=Path(__file__).resolve().parents[2]


class LongReportTimeoutTests(SimpleTestCase):
    def test_long_runtime_settings_are_accepted(self):
        values={'MCP_GATEWAY_URL':'http://gateway.example/mcp','MCP_TIMEOUT':'1500',
                'MCP_REQUEST_TIMEOUT':'1800','MCP_GATEWAY_TOKEN':''}
        with patch.object(gateway,'get_setting',side_effect=lambda key,default='':values.get(key,default)):
            config=gateway.load_gateway_config()
        self.assertEqual(config['timeout'],1500)
        self.assertEqual(config['request_timeout'],1800)
        self.assertGreater(IDEMPOTENCY_TTL,config['request_timeout'])

    def test_fallback_defaults_allow_twenty_minute_database_call(self):
        with patch.object(gateway,'get_setting',side_effect=lambda key,default='':
                          'http://gateway.example/mcp' if key=='MCP_GATEWAY_URL' else default):
            config=gateway.load_gateway_config()
        self.assertEqual(config['timeout'],1500)
        self.assertEqual(config['request_timeout'],1800)

    def test_invalid_or_over_profile_timeouts_still_fail(self):
        for key,value in [('MCP_TIMEOUT','1501'),('MCP_REQUEST_TIMEOUT','1801'),
                          ('MCP_TIMEOUT','nan'),('MCP_REQUEST_TIMEOUT','0')]:
            values={'MCP_GATEWAY_URL':'http://gateway.example/mcp','MCP_TIMEOUT':'1500',
                    'MCP_REQUEST_TIMEOUT':'1800',key:value}
            with self.subTest(key=key,value=value),patch.object(gateway,'get_setting',
                    side_effect=lambda k,default='':values.get(k,default)),self.assertRaises(gateway.GatewayError):
                gateway.load_gateway_config()

    def test_migration_updates_database_overrides_and_preserves_credentials(self):
        tree=ast.parse((ROOT/'deploy/migrate_release.py').read_text())
        assignments=[node for node in ast.walk(tree) if isinstance(node,ast.Assign)
                     and any(isinstance(t,ast.Name) and t.id=='updates' for t in node.targets)]
        updates=ast.literal_eval(assignments[0].value)
        self.assertEqual(updates['MCP_TIMEOUT'],'1500')
        self.assertEqual(updates['MCP_REQUEST_TIMEOUT'],'1800')
        self.assertNotIn('MCP_GATEWAY_TOKEN',updates)
        self.assertNotIn('MCP_MODEL',updates)

    def test_gateway_and_rollout_checks_use_the_same_profile(self):
        launcher=(ROOT/'deploy-agentic-updated.sh').read_text()
        self.assertIn('HTTPX_READ_TIMEOUT=1500',launcher)
        self.assertIn('assert value == 1500',launcher)
        self.assertIn("x.strip()=='1900s'",launcher)
        self.assertIn('DRAIN_DEADLINE=$((SECONDS+2100))',launcher)
        runtime=(ROOT/'deploy/runtime_verify.py').read_text()
        self.assertIn("config['timeout']==1500 and config['request_timeout']==1800",runtime)
        self.assertIn("(b'--timeout',b'1860')",runtime)
        verify=(ROOT/'deploy/verify_report_flow.py').read_text()
        self.assertIn('timeout=1920',verify)

    def test_previously_deployed_native_gateway_remains_exactly_recognized(self):
        from deploy.verify_gateway_source import verify
        baked=(ROOT/'agentic_ai/mcp_gateway.py').read_text()
        previous=baked.replace('"MCP_TIMEOUT", 1500, 1, 1500','"MCP_TIMEOUT", 30, 1, 300').replace(
            '"MCP_REQUEST_TIMEOUT", 1800, 1, 1800','"MCP_REQUEST_TIMEOUT", 90, 1, 600')
        known=(ROOT/'deploy/gateway_previous.sha256').read_text().splitlines()
        self.assertIn(hashlib.sha256(previous.encode()).hexdigest(),known)
        baseline=(ROOT/'deploy/gateway_baseline.sha256').read_text().strip()
        verify(previous,baked,baseline)
        with self.assertRaises(ValueError):verify(previous+'\nUNKNOWN=True\n',baked,baseline)
