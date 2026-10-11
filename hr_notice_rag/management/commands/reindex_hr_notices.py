from django.core.management.base import BaseCommand
from hr_notice_rag.models import HRNoticeDocument
from hr_notice_rag.services.pipeline import process_notice_upload


class Command(BaseCommand):
    help = "Reindex all HR notice DOCX files"

    def handle(self, *args, **options):
        qs = HRNoticeDocument.objects.all().order_by("id")
        count = 0
        for obj in qs:
            self.stdout.write(f"Reindexing document {obj.id} - {obj}")
            process_notice_upload(obj.id, force_reindex=True)
            count += 1
        self.stdout.write(self.style.SUCCESS(f"Done. Reindexed {count} documents."))
