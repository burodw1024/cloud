from django.conf import settings
from django.db import models


class HRNoticeDocument(models.Model):
    STATUS_CHOICES = [
        ("uploaded", "Uploaded"),
        ("processing", "Processing"),
        ("indexed", "Indexed"),
        ("failed", "Failed"),
    ]

    title = models.CharField(max_length=500, blank=True)
    notice_no = models.CharField(max_length=200, blank=True)
    department = models.CharField(max_length=200, blank=True)
    publish_date = models.DateField(null=True, blank=True)
    effective_date = models.DateField(null=True, blank=True)
    category = models.CharField(max_length=200, blank=True)

    file = models.FileField(upload_to="hr_notices/%Y/%m/")
    file_name = models.CharField(max_length=500, blank=True)

    template_type = models.CharField(max_length=100, blank=True)
    raw_text = models.TextField(blank=True)
    normalized_text = models.TextField(blank=True)

    indexing_notes = models.TextField(blank=True)
    index_status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="uploaded")

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="uploaded_hr_notices",
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self):
        return self.title or self.file_name or f"HR Notice #{self.pk}"


class HRNoticeChunk(models.Model):
    VECTOR_STATUS_CHOICES = [
        ("pending", "Pending"),
        ("indexed", "Indexed"),
        ("failed", "Failed"),
    ]

    document = models.ForeignKey(
        HRNoticeDocument,
        on_delete=models.CASCADE,
        related_name="chunks",
    )
    chunk_index = models.IntegerField()
    section_name = models.CharField(max_length=200, blank=True)
    chunk_text = models.TextField()
    qdrant_point_id = models.CharField(max_length=200, blank=True)
    vector_status = models.CharField(max_length=20, choices=VECTOR_STATUS_CHOICES, default="pending")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["document_id", "chunk_index"]
        unique_together = ("document", "chunk_index")

    def __str__(self):
        return f"Doc {self.document_id} / Chunk {self.chunk_index}"
