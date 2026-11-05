import time
from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ValidationError
from django.db import close_old_connections, OperationalError, IntegrityError
from chat.models import EmployeeSyncSettings
from chat.employee_sync import sync_employees, SyncBusy


class Command(BaseCommand):
    help = 'Sync NiFi employees once, or process queued and scheduled imports with --watch.'

    def add_arguments(self, parser):
        parser.add_argument('--watch', action='store_true')

    def handle(self, *args, **options):
        next_attempt = 0
        retry_after = 0
        while True:
            close_old_connections()
            interval = 300
            try:
                config = EmployeeSyncSettings.objects.filter(pk=1).first()
                if config:
                    interval = max(30, config.interval_seconds)
                requested = config and config.sync_requested
                scheduled = config and config.enabled and time.monotonic() >= next_attempt
                if (requested or scheduled) and time.monotonic() >= retry_after:
                    next_attempt = time.monotonic() + interval
                    self.stdout.write(sync_employees(progress=self.stdout.write))
                    # Measure the interval from completion, not from import start.
                    next_attempt = time.monotonic() + interval
                elif not options['watch']:
                    raise CommandError('Employee sync is not enabled or queued in admin settings.')
            except SyncBusy as exc:
                if not options['watch']:
                    raise CommandError(str(exc)) from exc
                self.stderr.write(str(exc))
            except (ValueError, ValidationError, OperationalError, IntegrityError) as exc:
                if not options['watch']:
                    raise CommandError(str(exc)) from exc
                next_attempt = retry_after = time.monotonic() + interval
                # Do not attempt another database write while SQLite is locked.
                self.stderr.write('Employee sync failed; retrying after the configured interval: ' + str(exc))
            if not options['watch']:
                return
            time.sleep(2)
