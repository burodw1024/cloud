import ast
from pathlib import Path
import re
import runpy
import unittest


class GatewayCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[2]
        cls.verifier = runpy.run_path(str(root/'deploy/verify_gateway_source.py'))
        cls.baked = (root/'agentic_ai/mcp_gateway.py').read_text()
        cls.legacy = (root/"agentic_ai/tests/fixtures/legacy_gateway.py.txt").read_text()
        cls.baseline = (root/'deploy/gateway_baseline.sha256').read_text().strip()

    def reported_live(self):
        live = self.legacy.replace('    # BEGIN_AGENTIC_OLLAMA_OPTIONS\n', '').replace('    # END_AGENTIC_OLLAMA_OPTIONS\n', '')
        return re.sub(r'(?m)^ *# BEGIN_AGENTIC_REPORT_PROJECTION\n.*?^ *# END_AGENTIC_REPORT_PROJECTION\n', '', live, flags=re.S|re.M)

    def test_reported_live_diff_is_accepted(self):
        self.verifier['verify'](self.reported_live(), self.baked, self.baseline)

    def test_added_empty_python_ast_metadata_does_not_change_identity(self):
        node = ast.parse('def example():\n    return None\n').body[0]
        canonical = self.verifier['canonical_node']
        before = canonical(node)
        node._fields = (*node._fields, 'new_empty_metadata')
        node.new_empty_metadata = []
        self.assertEqual(before, canonical(node))
        node.new_empty_metadata = ['meaningful change']
        self.assertNotEqual(before, canonical(node))

    def test_unrecognized_live_code_remains_blocked(self):
        with self.assertRaises(ValueError):
            self.verifier['verify'](self.reported_live() + '\nUNREVIEWED = True\n', self.baked, self.baseline)

    def test_changed_hotfix_options_remain_blocked(self):
        with self.assertRaises(ValueError):
            self.verifier['verify'](self.reported_live().replace('think=False', 'think=True'), self.baked, self.baseline)

    def test_exact_upgraded_source_is_accepted_but_a_modified_copy_is_not(self):
        self.verifier['verify'](self.baked, self.baked, self.baseline)
        with self.assertRaises(ValueError):
            self.verifier['verify'](self.baked + "\n# local unreviewed edit\n", self.baked, self.baseline)

    def test_legacy_deployed_projection_source_is_still_accepted(self):
        self.verifier['verify'](self.legacy, self.baked, self.baseline)

    def test_previously_published_member_gateway_is_exactly_recognized(self):
        previous = (Path(__file__).resolve().parent / 'fixtures/member_gateway.py.txt').read_text()
        self.verifier['verify'](previous, self.baked, self.baseline)
        with self.assertRaises(ValueError):
            self.verifier['verify'](previous + "\n# unknown edit\n", self.baked, self.baseline)

    def test_previously_published_usage_gateway_is_exactly_recognized(self):
        previous = (Path(__file__).resolve().parent / 'fixtures/usage_gateway.py.txt').read_text()
        self.verifier['verify'](previous, self.baked, self.baseline)
        with self.assertRaises(ValueError):
            self.verifier['verify'](previous + "\n# unknown edit\n", self.baked, self.baseline)
