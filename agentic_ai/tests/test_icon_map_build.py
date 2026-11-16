"""Guard the source-version-specific, cluster-independent image preparation."""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('icon_map_build', ROOT / 'deploy/superset/prepare_icon_map.py')
build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build)


class IconMapBuildTests(unittest.TestCase):
    def test_registration_adds_one_chart_without_replacing_existing_plugins(self):
        source = "import { isFeatureEnabled, FeatureFlag, Preset } from '@superset-ui/core';\nplugins: [new ExistingPlugin()],"
        result = build.register(source)
        self.assertIn("new BuroIconMapPlugin().configure({ key: 'buro_icon_map' })", result)
        self.assertIn('new ExistingPlugin()', result)
        self.assertEqual(result.count("key: 'buro_icon_map'"), 1)
        with self.assertRaises(ValueError):
            build.register(result)

    def test_other_upstream_structures_are_rejected(self):
        for source in ('plugins: []', '', "import { Preset } from '@superset-ui/core'; plugins: []"):
            with self.subTest(source=source), self.assertRaises(ValueError):
                build.register(source)
