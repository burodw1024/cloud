from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from agentic_ai.models import AgentRunLog


class Command(BaseCommand):
    help = "Verify audit inserts and updates without retaining a diagnostic record."
    requires_system_checks = []

    def handle(self, *args, **options):
        try:
            with transaction.atomic():
                audit = AgentRunLog.objects.create(
                    user_query="Audit storage readiness probe", status="failed",
                )
                AgentRunLog.objects.filter(pk=audit.pk).update(status="success")
                transaction.set_rollback(True)
        except Exception as exc:
            raise CommandError(
                "Audit storage check failed (%s). Apply migrations with this server's "
                "Django settings and check database permissions, disk space and locks."
                % type(exc).__name__
            ) from exc
        self.stdout.write(self.style.SUCCESS("Audit insert/update passed; probe rolled back."))
