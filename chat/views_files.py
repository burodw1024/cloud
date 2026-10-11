# chat/views_files.py
from __future__ import annotations

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import UploadedFile
from .file_extractors import extract_text

MAX_MB = 25

@csrf_exempt
@require_POST
def upload_file(request):
    user = getattr(request, "user", None)
    is_auth = bool(user and user.is_authenticated)

    f = request.FILES.get("file")
    if not f:
        return JsonResponse({"ok": False, "error": "file is required"}, status=400)

    if f.size > MAX_MB * 1024 * 1024:
        return JsonResponse({"ok": False, "error": f"File too large. Max {MAX_MB}MB."}, status=413)

    obj = UploadedFile.objects.create(
        user=user if is_auth else None,
        filename=f.name,
        content_type=getattr(f, "content_type", "") or "",
        file=f,
        extracted_text="",
    )

    # Extract text now (sync). If later you want async, use celery.
    try:
        path = obj.file.path
        text = extract_text(path, obj.content_type, obj.filename)
        obj.extracted_text = text[:2_000_000]  # safety cap
        obj.save(update_fields=["extracted_text"])
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Uploaded but extraction failed: {e}"}, status=502)

    return JsonResponse({
        "ok": True,
        "file_id": obj.id,
        "filename": obj.filename,
        "chars": len(obj.extracted_text or ""),
    })





from django.http import JsonResponse
from django.contrib.admin.views.decorators import staff_member_required
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_protect

@staff_member_required
@csrf_protect
@require_POST
def upload_jsonl(request):
    return JsonResponse({"ok": True, "msg": "upload_jsonl wired"})
