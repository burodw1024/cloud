from __future__ import annotations
import io, json, os, threading
from functools import wraps
from pathlib import Path
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from chat.dynamic_config import get_int_setting, get_setting


# --- OPENAI_POLICY_FIXED_ENV_BEGIN ---
os.environ["POLICY_WORK_DIR"] = str(
    getattr(
        settings,
        "POLICY_WORK_DIR",
        Path(settings.MEDIA_ROOT) / "policy_rag",
    )
)
os.environ["ADMIN_TOKEN"] = ""
# --- OPENAI_POLICY_FIXED_ENV_END ---

from .services.chat_service import ask_policy
from .services.tts_service import create_speech
from .services.legacy.compat import BackgroundTasks, UploadFile
from .services.legacy import admin as legacy_admin
from .services.legacy import preprocess as legacy_pre


# --- OPENAI_POLICY_RUNTIME_SYNC_BEGIN ---
def _sync_legacy_runtime():
    model = get_setting("OPENAI_MODEL", "").strip()

    legacy_admin.OPENAI_VECTOR_STORE_ID = get_setting(
        "OPENAI_VECTOR_STORE_ID", ""
    ).strip()
    legacy_admin.ADMIN_TOKEN = ""
    legacy_admin.ADMIN_MAX_UPLOAD_MB = get_int_setting(
        "ADMIN_MAX_UPLOAD_MB", 50
    )

    legacy_pre.OPENAI_PREPROCESS_MODEL = (
        get_setting("OPENAI_PREPROCESS_MODEL", model).strip() or model
    )
    legacy_pre.PREPROCESS_MAX_UPLOAD_MB = get_int_setting(
        "PREPROCESS_MAX_UPLOAD_MB", 50
    )
    legacy_pre.PREPROCESS_BATCH_CHARS = get_int_setting(
        "PREPROCESS_BATCH_CHARS", 12000
    )
# --- OPENAI_POLICY_RUNTIME_SYNC_END ---


def chat_permission_required(view_func):
    @wraps(view_func)
    @login_required
    def wrapped(request, *args, **kwargs):
        u = request.user
        if not (u.is_superuser or u.has_perm("openai_policy.use_policy_ai")): raise PermissionDenied
        return view_func(request, *args, **kwargs)
    return wrapped

def admin_permission_required(view_func):
    @wraps(view_func)
    @login_required
    def wrapped(request, *args, **kwargs):
        u = request.user
        if not (u.is_superuser or u.has_perm("openai_policy.manage_policy_rag") or u.has_perm("openai_policy.manage_policy_ai")): raise PermissionDenied
        _sync_legacy_runtime()
        return view_func(request, *args, **kwargs)
    return wrapped

def json_body(request): return json.loads((request.body.decode("utf-8") if request.body else "{}") or "{}")
def bool_value(v, default=False):
    if v is None: return default
    if isinstance(v, bool): return v
    return str(v).strip().lower() in {"1","true","yes","on"}
def error_response(exc):
    status = int(getattr(exc, "status_code", 502) or 502)
    detail = getattr(exc, "detail", None) or str(exc)
    if isinstance(exc, (ValueError, json.JSONDecodeError, UnicodeDecodeError)): status = 400
    return JsonResponse({"detail": str(detail)}, status=status)
def compat_upload(f): return UploadFile(file=io.BytesIO(f.read()), filename=f.name, content_type=getattr(f, "content_type", "") or "application/octet-stream")

@chat_permission_required
@require_GET
def chat_page(request): return render(request, "openai_policy/chat.html")

@admin_permission_required
@require_GET
def studio_page(request): return render(request, "openai_policy/studio.html")

@chat_permission_required
@require_POST
def chat_api(request):
    try:
        d=json_body(request); return JsonResponse(ask_policy(str(d.get("question","") or ""), d.get("previous_response_id")))
    except Exception as exc: return error_response(exc)

@chat_permission_required
@require_POST
def tts_api(request):
    try:
        d=json_body(request); audio=create_speech(str(d.get("text","") or ""), str(d.get("language","auto") or "auto"), d.get("voice")); r=HttpResponse(audio, content_type="audio/mpeg"); r["Cache-Control"]="no-store"; r["Content-Disposition"]='inline; filename="policy-reply.mp3"'; return r
    except Exception as exc: return error_response(exc)

@admin_permission_required
@require_GET
def admin_config(request):
    try: return JsonResponse(legacy_admin.admin_config(None))
    except Exception as exc: return error_response(exc)

@admin_permission_required
@require_GET
def admin_files(request):
    try: return JsonResponse(legacy_admin.list_policy_files(None))
    except Exception as exc: return error_response(exc)

@admin_permission_required
@require_POST
def admin_upsert(request):
    try:
        f=request.FILES.get("file")
        if not f: return JsonResponse({"detail":"file is required"}, status=400)
        return JsonResponse(legacy_admin.upsert_policy_file(file=compat_upload(f), document_key=request.POST.get("document_key",""), document_type=request.POST.get("document_type",""), version=request.POST.get("version",""), effective_date=request.POST.get("effective_date",""), title=request.POST.get("title",""), replace_existing=bool_value(request.POST.get("replace_existing"),True), x_admin_token=None))
    except Exception as exc: return error_response(exc)

@admin_permission_required
@require_http_methods(["DELETE"])
def admin_file_item(request, file_id):
    try: return JsonResponse(legacy_admin.delete_policy_file(file_id, None))
    except Exception as exc: return error_response(exc)

@admin_permission_required
@require_POST
def search_test(request):
    try: return JsonResponse(legacy_admin.search_test(query=request.POST.get("query",""), max_results=int(request.POST.get("max_results","5")), x_admin_token=None))
    except Exception as exc: return error_response(exc)

@admin_permission_required
@require_GET
def preprocess_config(request):
    try: return JsonResponse(legacy_pre.preprocess_config(None))
    except Exception as exc: return error_response(exc)

@admin_permission_required
@require_POST
def preprocess_create(request):
    try:
        f=request.FILES.get("file")
        if not f: return JsonResponse({"detail":"file is required"}, status=400)
        use_ai=bool_value(request.POST.get("use_openai_normalization"), True)
        result=legacy_pre.create_preprocess_job(background_tasks=BackgroundTasks(), file=compat_upload(f), document_key=request.POST.get("document_key",""), document_type=request.POST.get("document_type",""), version=request.POST.get("version",""), effective_date=request.POST.get("effective_date",""), title=request.POST.get("title",""), use_openai_normalization=use_ai, x_admin_token=None)
        threading.Thread(target=legacy_pre._run_preprocess, args=(result["job_id"],use_ai), daemon=True, name="policy-preprocess-%s"%result["job_id"]).start()
        result["poll_url"]="/policy-ai/api/admin/preprocess/%s"%result["job_id"]
        return JsonResponse(result, status=202)
    except Exception as exc: return error_response(exc)

@admin_permission_required
@require_GET
def preprocess_jobs(request):
    try: return JsonResponse(legacy_pre.list_preprocess_jobs(None))
    except Exception as exc: return error_response(exc)

@admin_permission_required
@require_http_methods(["GET","DELETE"])
def preprocess_item(request, job_id):
    try:
        return JsonResponse(legacy_pre.delete_preprocess_job(job_id,None) if request.method=="DELETE" else legacy_pre.get_preprocess_job(job_id,None))
    except Exception as exc: return error_response(exc)

@admin_permission_required
@require_POST
def preprocess_approve(request, job_id):
    try:
        d=json_body(request); obj=legacy_pre.ApproveRequest(approved=bool_value(d.get("approved"),True), note=str(d.get("note","") or "")[:1000]); return JsonResponse(legacy_pre.approve_preprocess_job(job_id,obj,None))
    except Exception as exc: return error_response(exc)

@admin_permission_required
@require_GET
def preprocess_artifact(request, job_id, artifact):
    try:
        job=legacy_pre._load_job(job_id); mapping={"source":job.get("source_artifact",""),"extracted":job.get("artifacts",{}).get("extracted",""),"unicode":job.get("artifacts",{}).get("unicode",""),"atomic":job.get("artifacts",{}).get("atomic",""),"markdown":job.get("artifacts",{}).get("markdown","")}; name=mapping.get(artifact,"")
        if not name: return JsonResponse({"detail":"Artifact is not available yet."},status=404)
        p=legacy_pre._job_dir(job_id)/name
        if not p.exists(): return JsonResponse({"detail":"Artifact file not found."},status=404)
        dl=bool_value(request.GET.get("download"),False)
        if dl or artifact=="source": return FileResponse(p.open("rb"),as_attachment=dl,filename=p.name)
        return HttpResponse(p.read_text(encoding="utf-8"),content_type="text/plain; charset=utf-8")
    except Exception as exc: return error_response(exc)

@admin_permission_required
@require_POST
def effective_build(request):
    try:
        d=json_body(request); obj=legacy_pre.BuildEffectiveRequest(job_ids=d.get("job_ids") or [], output_version=str(d.get("output_version","effective-current") or "effective-current")[:80], title=str(d.get("title","Organization Policy — Effective") or "Organization Policy — Effective")[:200]); return JsonResponse(legacy_pre.build_effective_policy(obj,None))
    except Exception as exc: return error_response(exc)

@admin_permission_required
@require_GET
def effective_builds(request):
    try: return JsonResponse(legacy_pre.list_effective_builds(None))
    except Exception as exc: return error_response(exc)

@admin_permission_required
@require_GET
def effective_item(request, build_id):
    try: return JsonResponse(legacy_pre.get_effective_build(build_id,None))
    except Exception as exc: return error_response(exc)

@admin_permission_required
@require_GET
def effective_artifact(request, build_id, artifact):
    try:
        legacy_pre._load_build(build_id); name={"markdown":"effective.md","atomic":"effective.jsonl"}.get(artifact,"")
        if not name: return JsonResponse({"detail":"Unknown effective artifact."},status=404)
        p=legacy_pre._build_dir(build_id)/name
        if not p.exists(): return JsonResponse({"detail":"Artifact not found."},status=404)
        if bool_value(request.GET.get("download"),False): return FileResponse(p.open("rb"),as_attachment=True,filename=p.name)
        return HttpResponse(p.read_text(encoding="utf-8"),content_type="text/plain; charset=utf-8")
    except Exception as exc: return error_response(exc)

@admin_permission_required
@require_POST
def effective_publish(request, build_id):
    try:
        d=json_body(request); obj=legacy_pre.PublishEffectiveRequest(replace_active_effective=bool_value(d.get("replace_active_effective"),True)); return JsonResponse(legacy_pre.publish_effective_policy(build_id,obj,None))
    except Exception as exc: return error_response(exc)
