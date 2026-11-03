"""Run regularly to enforce snapshot retention and source-access revocation."""
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from agentic_ai.dashboard_studio import current_authorization
from agentic_ai.models import DashboardExport
from agentic_ai.superset_publisher import configuration, remove


class Command(BaseCommand):
    help = 'Remove revoked or expired private Superset exports (dry run unless --apply).'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--days', type=int, default=7)

    def handle(self, *args, **options):
        if options['days'] < 1:
            raise CommandError('Retention must be at least one day.')
        cutoff = timezone.now() - timedelta(days=options['days'])
        abandoned = DashboardExport.objects.filter(status='pending', created_at__lt=cutoff)
        self.stdout.write(f'{abandoned.count()} expired unsubmitted previews')
        if options['apply']:
            abandoned.delete()
        failures = 0
        for export in DashboardExport.objects.select_related('user').filter(status__in=['ready', 'failed']):
            if export.created_at > cutoff and current_authorization(export.user_id, export.source):
                continue
            self.stdout.write(f'{export.pk}: remove revoked or expired snapshot')
            if not options['apply']:
                continue
            try:
                config = configuration(export.user)
                from agentic_ai.dashboard_studio import binding
                if export.source.get('binding') != binding(config):
                    raise ValueError('Destination changed')
                if not DashboardExport.objects.filter(pk=export.pk, status__in=['ready', 'failed']).update(status='creating'):
                    continue
                remove(export, config)
            except Exception:
                failures += 1
                DashboardExport.objects.filter(pk=export.pk).update(status='failed', error='Retention cleanup needs administrator review.')
                self.stderr.write(f'{export.pk}: cleanup needs administrator review')
        if failures:
            raise CommandError(f'{failures} export cleanup operations need review.')
