import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class PulledSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = Path(__file__).resolve().parents[2]
        (self.root/'deploy').mkdir()
        for name in ('deploy/source_checkout.py', 'deploy-agentic-updated.sh'):
            shutil.copy2(self.project/name, self.root/name)
        self.git('init', '-q')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('remote', 'add', 'origin', 'https://github.com/burodw1024/cloud.git')
        self.git('add', '.')
        self.git('commit', '-qm', 'test source')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], text=True).strip()

    def check(self):
        return subprocess.run(['bash', str(self.root/'deploy-agentic-updated.sh'), '--check-source'],
                              cwd=self.root, capture_output=True, text=True)

    def test_checks_pulled_commit_without_cluster_or_build(self):
        before = self.git('rev-parse', 'HEAD')
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(before, result.stdout)
        self.assertEqual(before, self.git('rev-parse', 'HEAD'))
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_local_changes_are_preserved_and_block_deployment(self):
        file = self.root/'local-work.txt'
        file.write_text('keep this work')
        result = self.check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('local changes', result.stderr)
        self.assertEqual(file.read_text(), 'keep this work')

    def test_gitea_checkout_is_rejected_without_network_access(self):
        self.git('remote', 'set-url', 'origin', 'http://gitea.example/repository.git')
        result = self.check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('GitHub as origin', result.stderr)

    def test_gateway_hotfix_is_baked_and_unknown_changes_are_blocked(self):
        import runpy
        verifier = runpy.run_path(str(self.project/'deploy/verify_gateway_source.py'))
        baked = (self.project/'agentic_ai/mcp_gateway.py').read_text()
        baseline = (self.project/'deploy/gateway_baseline.sha256').read_text().strip()
        verifier['verify'](baked, baked, baseline)
        with self.assertRaises(ValueError):
            verifier['verify'](baked + '\nUNREVIEWED_CHANGE = True\n', baked, baseline)
