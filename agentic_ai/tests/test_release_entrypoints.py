import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest


class ReleaseEntrypointTests(unittest.TestCase):
    def test_absolute_entrypoints_find_project_from_outside_directory(self):
        root = Path(__file__).resolve().parents[2]
        for name in ('runtime_verify.py', 'migrate_release.py'):
            with self.subTest(script=name), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                project = base/'app'
                (project/'deploy').mkdir(parents=True)
                (project/'django').mkdir()
                (project/'buro_first').mkdir()
                (project/'buro_first/__init__.py').write_text('')
                (project/'django/__init__.py').write_text(
                    "def setup():\n    import buro_first\n    print('PROJECT_IMPORT_SUCCEEDED')\n    raise SystemExit(0)\n")
                target = project/'deploy'/name
                target.write_text((root/'deploy'/name).read_text())
                data = base/'data'
                data.mkdir()
                with sqlite3.connect(data/'db.sqlite3') as database:
                    database.execute('CREATE TABLE example (id INTEGER)')
                wrapper = '''
import pathlib, runpy, subprocess, sys
from unittest.mock import patch
real_path = pathlib.Path
data, target = sys.argv[1:]
def mapped(value, *args):
    return real_path(data) if str(value) == '/data' else real_path(value, *args)
with patch('pathlib.Path', side_effect=mapped), patch('subprocess.run'):
    runpy.run_path(target, run_name='__main__')
'''
                env = dict(os.environ, AGENT_RELEASE_ID='entrypoint-test')
                env.pop('PYTHONPATH', None)
                result = subprocess.run([sys.executable, '-c', wrapper, str(data), str(target)],
                                        cwd=base, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('PROJECT_IMPORT_SUCCEEDED', result.stdout)
                if name == 'migrate_release.py':
                    self.assertTrue((data/'deployment-backups/entrypoint-test/db.sqlite3').is_file())
