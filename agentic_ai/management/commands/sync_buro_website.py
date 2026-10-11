from django.core.management.base import BaseCommand
from agentic_ai.website_sync.sync_buro_website import sync


class Command(BaseCommand):
    help = "Sync BURO website pages into Qdrant collection buro_website"

    def handle(self, *args, **options):
        result = sync()
        self.stdout.write(self.style.SUCCESS(str(result)))
