# chat/file_upload.py
import json
import os
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from .models import UploadedFile

ALLOWED_EXT = {".pdf", ".txt", ".docx", ".json", ".png", ".jpg", ".jpeg",".xls",".xlsx"}
MAX_MB = int(os.getenv("UPLOAD_MAX_MB", "25") or "25")


def _extract_text_txt(file_path: str) -> str:
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        try:
            with open(file_path, "rb") as f:
                return f.read().decode("utf-8", errors="replace")
        except Exception:
            return ""


def _extract_text_json(file_path: str) -> str:
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            obj = json.load(f)
        return json.dumps(obj, ensure_ascii=False, indent=2)
    except Exception:
        return _extract_text_txt(file_path)


def _extract_text_docx(file_path: str) -> str:
    try:
        import docx  # python-docx
        d = docx.Document(file_path)
        parts = []
        for p in d.paragraphs:
            if p.text:
                parts.append(p.text)
        return "\n".join(parts)
    except Exception:
        return ""


def _extract_text_pdf(file_path: str) -> str:
    parts = []

    # Try pdfplumber
    try:
        import pdfplumber
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                t = page.extract_text() or ""
                if t.strip():
                    parts.append(t)
        out = "\n\n".join(parts).strip()
        if out:
            return out
    except Exception:
        pass

    # Fallback PyPDF2
    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(file_path)
        for p in reader.pages:
            try:
                t = p.extract_text() or ""
            except Exception:
                t = ""
            if t.strip():
                parts.append(t)
        return "\n\n".join(parts).strip()
    except Exception:
        return ""


def _extract_text_by_ext(ext: str, file_path: str) -> str:
    ext = (ext or "").lower()
    if ext == ".txt":
        return _extract_text_txt(file_path)
    if ext == ".json":
        return _extract_text_json(file_path)
    if ext == ".docx":
        return _extract_text_docx(file_path)
    if ext == ".pdf":
        return _extract_text_pdf(file_path)
    # images: no OCR here
    return ""


@csrf_exempt
def upload_file(request):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required"}, status=405)

    f = request.FILES.get("file")
    if not f:
        return JsonResponse({"ok": False, "error": "No file uploaded (field name must be 'file')"}, status=400)

    ext = os.path.splitext(f.name)[1].lower()
    if ext not in ALLOWED_EXT:
        return JsonResponse({"ok": False, "error": "Unsupported file type: %s" % ext}, status=400)

    if f.size > MAX_MB * 1024 * 1024:
        return JsonResponse({"ok": False, "error": "File too large. Max %s MB" % MAX_MB}, status=400)

    user = getattr(request, "user", None)
    if not user or not getattr(user, "is_authenticated", False):
        user = None

    # Save
    uf = UploadedFile.objects.create(
        user=user,
        filename=f.name,
        content_type=getattr(f, "content_type", "") or "",
        file=f,
        extracted_text="",
    )

    # Extract
    extracted_text = ""
    try:
        stored_path = uf.file.path
        extracted_text = _extract_text_by_ext(ext, stored_path) or ""
    except Exception:
        extracted_text = ""

    extracted_text = extracted_text[:200000]
    uf.extracted_text = extracted_text
    uf.save(update_fields=["extracted_text"])

    # ✅ IMPORTANT: return UUID file_id, not numeric id
    return JsonResponse(
        {
            "ok": True,
            "file_id": uf.file_id,   # ✅ UUID token the UI must store/use
            "db_id": uf.id,          # optional debug
            "filename": uf.filename,
            "media_path": uf.file.url,
            "text_len": len(extracted_text),
        }
    )
