from django.core.management.base import BaseCommand
from hr_notice_rag.models import HRNoticeDocument


class Command(BaseCommand):
    help = "Print latest extracted HR notice records"

    def handle(self, *args, **options):
        qs = HRNoticeDocument.objects.all().order_by("-id")[:10]
        for obj in qs:
            self.stdout.write("=" * 80)
            self.stdout.write(f"ID: {obj.id}")
            self.stdout.write(f"Title: {obj.title}")
            self.stdout.write(f"Notice No: {obj.notice_no}")
            self.stdout.write(f"Department: {obj.department}")
            self.stdout.write(f"Publish Date: {obj.publish_date}")
            self.stdout.write(f"Template: {obj.template_type}")
            self.stdout.write(f"Status: {obj.index_status}")
            self.stdout.write(f"Normalized Preview: {(obj.normalized_text or '')[:500]}")
