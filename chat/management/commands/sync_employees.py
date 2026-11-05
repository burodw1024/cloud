import time
from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ValidationError
from django.db import close_old_connections
from chat.models import EmployeeSyncSettings
from chat.employee_sync import sync_employees


class Command(BaseCommand):
    help = 'Sync NiFi employees once, or continuously with --watch.'

    def add_arguments(self, parser):
        parser.add_argument('--watch', action='store_true')

    def handle(self, *args, **options):
        while True:
            close_old_connections()
            config = EmployeeSyncSettings.objects.filter(pk=1).first()
            try:
                if config and config.enabled:
                    self.stdout.write(sync_employees())
                elif not options['watch']:
                    raise CommandError('Employee sync is not enabled in admin settings.')
            except (ValueError, ValidationError) as exc:
                if not options['watch']:
                    raise CommandError(str(exc)) from exc
                EmployeeSyncSettings.objects.filter(pk=1).update(last_result='Sync failed: ' + str(exc))
                self.stderr.write(str(exc))
            if not options['watch']:
                return
            time.sleep(max(30, config.interval_seconds if config else 300))
