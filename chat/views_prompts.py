import json
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods

from .models import SavedPrompt


def _bad(msg: str, status: int = 400) -> JsonResponse:
    return JsonResponse({"ok": False, "error": msg}, status=status)


@login_required
@csrf_protect
@require_http_methods(["GET"])
def prompts_list(request):
    items = list(
        SavedPrompt.objects.filter(owner=request.user).values(
            "id", "title", "body", "created_at", "updated_at"
        )
    )
    return JsonResponse({"ok": True, "items": items})


@login_required
@csrf_protect
@require_http_methods(["POST"])
def prompts_save(request):
    try:
        data = json.loads(request.body.decode("utf-8"))
    except Exception:
        return _bad("Invalid JSON.")

    title = (data.get("title") or "").strip()
    body = (data.get("body") or "").strip()

    if not title or not body:
        return _bad("title and body are required.")

    obj, created = SavedPrompt.objects.update_or_create(
        owner=request.user,
        title=title,
        defaults={"body": body},
    )
    return JsonResponse({"ok": True, "id": obj.id, "created": created})


@login_required
@csrf_protect
@require_http_methods(["DELETE"])
def prompts_delete(request, prompt_id: int):
    deleted, _ = SavedPrompt.objects.filter(owner=request.user, id=prompt_id).delete()
    if not deleted:
        return _bad("Not found.", status=404)
    return JsonResponse({"ok": True})
