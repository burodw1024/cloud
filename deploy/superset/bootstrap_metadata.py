"""One-shot initialization, dispatched only by initialize_empty_db.py."""
import os
from importlib.metadata import version
import subprocess

import psycopg2

MONITORING_VIEWS = {
    ('metric_helpers', 'index_bloat', 'v', None),
    ('metric_helpers', 'nearly_exhausted_sequences', 'v', None),
    ('metric_helpers', 'pg_stat_statements', 'v', None),
    ('metric_helpers', 'table_bloat', 'v', None),
    ('public', 'pg_stat_kcache', 'v', 'pg_stat_kcache'),
    ('public', 'pg_stat_kcache_detail', 'v', 'pg_stat_kcache'),
    ('public', 'pg_stat_statements', 'v', 'pg_stat_statements'),
    ('public', 'pg_stat_statements_info', 'v', 'pg_stat_statements'),
}


def blocking_objects(rows):
    return [row for row in rows if tuple(row) not in MONITORING_VIEWS]


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def main():
    require(version('apache-superset') == '4.1.2', 'Expected the prepared Superset 4.1.2 image')
    require(os.environ['POSTGRES_HOST'] == '192.168.1.223'
            and os.environ['POSTGRES_DB'] == 'superset'
            and os.environ['POSTGRES_USER'] == 'supersetadmin', 'Unexpected metadata database destination')
    connection = psycopg2.connect(
        host=os.environ['POSTGRES_HOST'], port=int(os.environ['POSTGRES_PORT']),
        dbname=os.environ['POSTGRES_DB'], user=os.environ['POSTGRES_USER'],
        password=os.environ['POSTGRES_PASSWORD'], sslmode=os.environ['POSTGRES_SSLMODE'],
        connect_timeout=5)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_try_advisory_lock(17632086)')
            require(cursor.fetchone()[0], 'Another metadata initialization is running')
            cursor.execute("""SELECT n.nspname, c.relname, c.relkind, e.extname FROM pg_class c
                JOIN pg_namespace n ON n.oid=c.relnamespace
                LEFT JOIN pg_depend d ON d.classid='pg_class'::regclass AND d.objid=c.oid
                    AND d.deptype='e' AND d.refclassid='pg_extension'::regclass
                LEFT JOIN pg_extension e ON e.oid=d.refobjid
                WHERE c.relkind IN ('r','p','m','f','v')
                AND n.nspname NOT IN ('pg_catalog','information_schema')
                AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp%'""")
            objects = cursor.fetchall()
            blocked = blocking_objects(objects)
            require(not blocked, 'Database has unrecognized objects; no migrations or account resets were run: '+repr(blocked[:10]))
            print('Verified monitoring views retained:', len(objects), flush=True)
        print('Verified empty metadata database; initializing Superset 4.1.2.', flush=True)
        subprocess.run(['superset', 'db', 'upgrade'], check=True)
        from superset.app import create_app
        from superset.extensions import appbuilder
        app = create_app()
        with app.app_context():
            manager = appbuilder.sm
            username = os.environ['BOOTSTRAP_ADMIN_USERNAME']
            require(not manager.find_user(username=username), 'Admin already exists; refusing password reset')
            user = manager.add_user(
                username=username, first_name='Superset', last_name='Admin',
                email=os.environ['BOOTSTRAP_ADMIN_EMAIL'], role=manager.find_role('Admin'),
                password=os.environ['BOOTSTRAP_ADMIN_PASSWORD'])
            require(user, 'Could not create initial web administrator')
        subprocess.run(['superset', 'init'], check=True)
        print('Superset metadata and initial web administrator created.', flush=True)
    finally:
        connection.close()  # Releases the session advisory lock.


if __name__ == '__main__':
    main()
