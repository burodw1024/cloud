from hr_notice_rag.models import HRNoticeDocument, HRNoticeChunk
from hr_notice_rag.services.docx_reader import read_docx_blocks, blocks_to_raw_text
from hr_notice_rag.services.template_detector import detect_template_type
from hr_notice_rag.services.normalizer import normalize_blocks
from hr_notice_rag.services.field_extractor import extract_fields
from hr_notice_rag.services.chunker import chunk_notice_text
from hr_notice_rag.services.embedder import embed_texts
from hr_notice_rag.services.qdrant_indexer import delete_document_chunks, upsert_document_chunks


def process_notice_upload(document_id: int, force_reindex: bool = False):
    document = HRNoticeDocument.objects.get(pk=document_id)

    document.index_status = "processing"
    document.indexing_notes = ""
    document.save(update_fields=["index_status", "indexing_notes", "updated_at"])

    file_path = document.file.path
    blocks = read_docx_blocks(file_path)
    raw_text = blocks_to_raw_text(blocks)
    template_type = detect_template_type(blocks)
    normalized = normalize_blocks(blocks)

    fields = extract_fields(raw_text, normalized["normalized_text"])

    document.raw_text = raw_text
    document.normalized_text = normalized["normalized_text"]
    document.template_type = template_type
    document.file_name = document.file.name

    if fields.get("title"):
        document.title = fields["title"]
    if fields.get("notice_no"):
        document.notice_no = fields["notice_no"]
    if fields.get("publish_date"):
        document.publish_date = fields["publish_date"]
    if fields.get("department"):
        document.department = fields["department"]

    document.save()

    chunks = chunk_notice_text(
        title=document.title,
        notice_no=document.notice_no,
        publish_date=document.publish_date,
        normalized_text=document.normalized_text,
        max_words=700,
        overlap_words=100,
    )

    if not chunks:
        document.index_status = "failed"
        document.indexing_notes = "No chunkable text extracted from DOCX"
        document.save(update_fields=["index_status", "indexing_notes", "updated_at"])
        raise RuntimeError("No chunkable text extracted from DOCX")

    try:
        if force_reindex or document.chunks.exists():
            try:
                delete_document_chunks(document.id)
            except Exception:
                pass
            document.chunks.all().delete()

        vectors = embed_texts([chunk["chunk_text"] for chunk in chunks])
        point_ids = upsert_document_chunks(document, chunks, vectors)

        for chunk, point_id in zip(chunks, point_ids):
            HRNoticeChunk.objects.create(
                document=document,
                chunk_index=chunk["chunk_index"],
                section_name=chunk.get("section_name", ""),
                chunk_text=chunk["chunk_text"],
                qdrant_point_id=point_id,
                vector_status="indexed",
            )

        document.index_status = "indexed"
        document.indexing_notes = f"Indexed {len(chunks)} chunks"
        document.save(update_fields=["index_status", "indexing_notes", "updated_at"])
        return document

    except Exception as e:
        document.index_status = "failed"
        document.indexing_notes = str(e)
        document.save(update_fields=["index_status", "indexing_notes", "updated_at"])
        raise
