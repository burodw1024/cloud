import ast
from pathlib import Path
import runpy
import tempfile
from django.test import SimpleTestCase

ROOT=Path(__file__).resolve().parents[2]
PATCH=runpy.run_path(str(ROOT/'deploy/contextforge-timeout/patch_read_timeout.py'))['patch']
SAMPLE='''class Settings:
    httpx_read_timeout: float = Field(default=120.0, ge=1.0, le=600.0)
    httpx_write_timeout: float = Field(default=30.0, ge=1.0, le=600.0)
'''


class ContextForgeTimeoutCompatibilityTests(SimpleTestCase):
    def test_known_bound_is_changed_without_touching_other_limits(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'config.py';path.write_text(SAMPLE)
            PATCH(path)
            expected=SAMPLE.replace('default=120.0, ge=1.0, le=600.0','default=120.0, ge=1.0, le=1800.0')
            self.assertEqual(path.read_text(),expected)
            ast.parse(path.read_text())

    def test_unknown_source_is_rejected_without_rewriting_it(self):
        for value in (SAMPLE.replace('le=600.0','le=700.0'),SAMPLE.replace('httpx_read_timeout','other_timeout')):
            with self.subTest(source=value),tempfile.TemporaryDirectory() as temp:
                path=Path(temp)/'config.py';path.write_text(value)
                with self.assertRaises(ValueError):PATCH(path)
                self.assertEqual(path.read_text(),value)

    def test_gateway_preflight_precedes_deployment_mutation(self):
        source=(ROOT/'deploy-agentic-updated.sh').read_text()
        probe=source.index('env HTTPX_READ_TIMEOUT=1500')
        mutate=source.index('kg set env deployment/contextforge')
        self.assertLess(probe,mutate)
        self.assertIn('No gateway deployment change was made',source)
        self.assertLess(mutate,source.index('k scale deployment buro-django --replicas=0'))
