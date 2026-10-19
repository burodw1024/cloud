import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from django.test import SimpleTestCase
from agentic_ai.sqlite_cache import SQLiteCache


class SQLiteCacheTests(SimpleTestCase):
    def test_instances_share_atomic_claims_and_counters(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / 'cache.sqlite3')
            cache = SQLiteCache(path, {})
            other = SQLiteCache(path, {})
            cache.set('counter', 0)
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda _: other.incr('counter'), range(40)))
                claims = list(pool.map(lambda _: other.add('claim', 'pending'), range(20)))
            self.assertEqual(cache.get('counter'), 40)
            self.assertEqual(sum(claims), 1)
            self.assertEqual(cache.get('claim'), 'pending')
            self.assertFalse(cache.add('claim', 'replacement'))
            cache.set('expired', 3, timeout=-1)
            self.assertIsNone(other.get('expired'))
            self.assertTrue(other.add('expired', 4))
            self.assertEqual(cache.incr('expired'), 5)
            self.assertTrue(cache.delete('expired'))
            with self.assertRaises(ValueError):
                cache.incr('expired')
            cache.clear()
            self.assertIsNone(other.get('counter'))
