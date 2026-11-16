import json
from pathlib import Path
import runpy
import sys
import tempfile
import tomllib
import unittest
from types import SimpleNamespace
from unittest.mock import patch


class SupersetRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helper = runpy.run_path(str(Path(__file__).resolve().parents[2]/'deploy/superset/configure_registry.py'))

    def test_modern_registry_empty_path_with_single_quotes_preserves_other_settings(self):
        source = '''version = 3
[plugins.'io.containerd.cri.v1.images'.registry]
  config_path = '' # previously unset
[plugins.'io.containerd.cri.v1.runtime'.containerd]
  default_runtime_name = "nvidia"
'''
        result = self.helper['enable_registry_path'](source, '/etc/containerd/certs.d')
        parsed = tomllib.loads(result)
        self.assertEqual(parsed['plugins']['io.containerd.cri.v1.images']['registry']['config_path'], '/etc/containerd/certs.d')
        self.assertEqual(parsed['plugins']['io.containerd.cri.v1.runtime']['containerd']['default_runtime_name'], 'nvidia')

    def test_legacy_registry_path_and_missing_registry_section(self):
        for source in ('''version = 2
[plugins."io.containerd.grpc.v1.cri".registry]
  config_path = ""
[plugins."io.containerd.grpc.v1.cri".containerd]
  snapshotter = "overlayfs"
''', '''version = 2
[plugins."io.containerd.grpc.v1.cri".containerd]
  snapshotter = "overlayfs"
'''):
            result = self.helper['enable_registry_path'](source, '/etc/containerd/certs.d')
            parsed = tomllib.loads(result)
            self.assertEqual(parsed['plugins']['io.containerd.grpc.v1.cri']['registry']['config_path'], '/etc/containerd/certs.d')
            self.assertEqual(parsed['plugins']['io.containerd.grpc.v1.cri']['containerd']['snapshotter'], 'overlayfs')

    def test_imported_or_inline_configuration_requires_inspection(self):
        for source in ('version=3\nimports=["conf.d/*.toml"]\n',
                       'version=3\n[plugins."io.containerd.cri.v1.images"]\nregistry={config_path=""}\n'):
            with self.assertRaises(RuntimeError):
                self.helper['enable_registry_path'](source, '/etc/containerd/certs.d')

    def test_existing_effective_path_is_reused(self):
        source = '''version=3
[plugins."io.containerd.cri.v1.images".registry]
config_path="/custom/hosts:/etc/containerd/certs.d"
'''
        self.assertEqual(self.helper['registry_path'](source), '/custom/hosts')

    def test_wrong_machine_blocks_before_configuration_or_runtime_mutation(self):
        with patch.object(sys, 'argv', ['configure_registry.py']), patch('os.geteuid', return_value=0), \
             patch('shutil.which', return_value='/usr/bin/tool'), \
             patch('subprocess.check_output', return_value=json.dumps([{'addr_info': [{'local': '192.168.1.144'}]}])), \
             patch('subprocess.run') as run:
            with self.assertRaisesRegex(RuntimeError, 'Wrong machine'):
                self.helper['main']()
            run.assert_not_called()

    def test_http_host_configuration_is_scoped_to_the_registry(self):
        hosts = tomllib.loads(self.helper['HOSTS'])
        self.assertEqual(hosts['server'], 'http://192.168.1.224:80')
        self.assertEqual(list(hosts['host']), ['http://192.168.1.224:80'])
        self.assertEqual(hosts['host'][hosts['server']]['capabilities'], ['pull', 'resolve'])

    def test_existing_disk_path_still_restarts_daemon_before_cri_pull(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            hosts = root/'certs'
            config = root/'config.toml'
            original = 'version=3\n[plugins."io.containerd.cri.v1.images".registry]\nconfig_path='+json.dumps(str(hosts))+'\n'
            config.write_text(original)
            def output(command, **kwargs):
                if command[0] == 'ip':
                    return json.dumps([{'addr_info': [{'local': '192.168.1.176'}]}])
                return original
            calls = []
            def run(command, **kwargs):
                calls.append(command)
                return SimpleNamespace(returncode=0)
            main = self.helper['main']
            with patch.object(sys, 'argv', ['configure_registry.py']), patch('os.geteuid', return_value=0), \
                 patch('shutil.which', return_value='/usr/bin/tool'), \
                 patch.dict(main.__globals__, {'active_config': lambda: config}), \
                 patch('subprocess.check_output', side_effect=output), patch('subprocess.run', side_effect=run):
                main()
            restart = calls.index(['systemctl', 'restart', 'containerd'])
            pull = next(i for i, command in enumerate(calls) if 'pull' in command)
            self.assertLess(restart, pull)
            self.assertEqual(config.read_text(), original)
            self.assertTrue(list(root.glob('config.toml.backup-*')))
            self.assertTrue((hosts/'192.168.1.224:80/hosts.toml').is_file())
