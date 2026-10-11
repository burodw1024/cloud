from django.contrib import admin
from .models import HRNoticeDocument, HRNoticeChunk


@admin.register(HRNoticeDocument)
class HRNoticeDocumentAdmin(admin.ModelAdmin):
    list_display = (
        "id", "title", "notice_no", "department",
        "publish_date", "index_status", "uploaded_at"
    )
    list_filter = ("index_status", "department", "publish_date", "uploaded_at")
    search_fields = ("title", "notice_no", "department", "file_name", "raw_text", "normalized_text")
    readonly_fields = ("uploaded_at", "updated_at")


@admin.register(HRNoticeChunk)
class HRNoticeChunkAdmin(admin.ModelAdmin):
    list_display = ("id", "document", "chunk_index", "section_name", "vector_status", "qdrant_point_id")
    list_filter = ("vector_status",)
    search_fields = ("chunk_text", "section_name", "qdrant_point_id")
