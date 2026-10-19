"""Shared, atomic cache for the single-node SQLite deployment."""
from contextlib import contextmanager
import pickle
import sqlite3
import time
from pathlib import Path

from django.core.cache.backends.base import BaseCache, DEFAULT_TIMEOUT


class SQLiteCache(BaseCache):
    def __init__(self, location, params):
        super().__init__(params)
        self.location = location

    @contextmanager
    def _connect(self):
        Path(self.location).parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.location, timeout=20)
        try:
            with connection:
                connection.execute('CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value BLOB NOT NULL, expires REAL)')
                yield connection
        finally:
            connection.close()

    def _expiry(self, timeout):
        timeout = self.default_timeout if timeout is DEFAULT_TIMEOUT else timeout
        return None if timeout is None else time.time() + timeout

    def get(self, key, default=None, version=None):
        with self._connect() as db:
            row = db.execute('SELECT value FROM cache WHERE key=? AND (expires IS NULL OR expires>?)',
                             (self.make_and_validate_key(key, version=version), time.time())).fetchone()
        return pickle.loads(row[0]) if row else default

    def set(self, key, value, timeout=DEFAULT_TIMEOUT, version=None):
        with self._connect() as db:
            db.execute('INSERT INTO cache VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, expires=excluded.expires',
                       (self.make_and_validate_key(key, version=version), pickle.dumps(value), self._expiry(timeout)))
            db.execute('DELETE FROM cache WHERE expires IS NOT NULL AND expires<=?', (time.time(),))

    def add(self, key, value, timeout=DEFAULT_TIMEOUT, version=None):
        with self._connect() as db:
            result = db.execute('INSERT INTO cache VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, expires=excluded.expires WHERE cache.expires<=?',
                                (self.make_and_validate_key(key, version=version), pickle.dumps(value), self._expiry(timeout), time.time()))
            return result.rowcount == 1

    def incr(self, key, delta=1, version=None):
        key = self.make_and_validate_key(key, version=version)
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT value FROM cache WHERE key=? AND (expires IS NULL OR expires>?)', (key, time.time())).fetchone()
            if row is None:
                raise ValueError('Key not found')
            value = pickle.loads(row[0]) + delta
            db.execute('UPDATE cache SET value=? WHERE key=?', (pickle.dumps(value), key))
            return value

    def delete(self, key, version=None):
        with self._connect() as db:
            return db.execute('DELETE FROM cache WHERE key=?', (self.make_and_validate_key(key, version=version),)).rowcount > 0

    def clear(self):
        with self._connect() as db:
            db.execute('DELETE FROM cache')
        return True
