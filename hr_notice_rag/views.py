from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from .forms import HRNoticeUploadForm
from .models import HRNoticeDocument
from .services.pipeline import process_notice_upload
from .services.retriever import retrieve_notice_context


@login_required
@require_http_methods(["GET", "POST"])
def upload_notice(request):
    if request.method == "POST":
        form = HRNoticeUploadForm(request.POST, request.FILES)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.uploaded_by = request.user
            obj.file_name = obj.file.name
            obj.index_status = "uploaded"
            obj.save()

            try:
                process_notice_upload(obj.id)
            except Exception as e:
                obj.index_status = "failed"
                obj.indexing_notes = str(e)
                obj.save(update_fields=["index_status", "indexing_notes"])

            return redirect("hr_notice_rag:detail", pk=obj.pk)
    else:
        form = HRNoticeUploadForm()
    return render(request, "hr_notice_rag/upload.html", {"form": form})


@login_required
def notice_list(request):
    notices = HRNoticeDocument.objects.all()[:200]
    return render(request, "hr_notice_rag/list.html", {"notices": notices})


@login_required
def notice_detail(request, pk):
    obj = get_object_or_404(HRNoticeDocument, pk=pk)
    return render(request, "hr_notice_rag/detail.html", {"obj": obj})


@login_required
@require_http_methods(["POST"])
def reindex_notice(request, pk):
    obj = get_object_or_404(HRNoticeDocument, pk=pk)
    try:
        process_notice_upload(obj.id, force_reindex=True)
        return JsonResponse({"ok": True, "message": "Reindexed successfully", "document_id": obj.id})
    except Exception as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=500)


@login_required
@require_http_methods(["GET"])
def search_notice_context(request):
    q = (request.GET.get("q") or "").strip()
    if not q:
        return HttpResponseBadRequest("Missing q parameter")

    results = retrieve_notice_context(q, top_k=5)
    return JsonResponse({"ok": True, "query": q, "results": results})
