import json

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import WorkflowSession
from .services.repository import get_active_definition, get_active_workflow
from .services.intent_detector import detect_intent
from .services.command_parser import (
    parse_change_command,
    is_resume_command,
    is_cancel_command,
    is_handoff_request,
)
from .services.engine import WorkflowEngine
from .services.permissions import user_can_start_workflow


@csrf_exempt
@require_POST
def workflow_message_api(request):
    if not request.user.is_authenticated:
        return JsonResponse({"ok": False, "error": "Authentication required"}, status=401)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"ok": False, "error": "Invalid JSON body"}, status=400)

    session_id = (payload.get("session_id") or "").strip()
    message = (payload.get("message") or "").strip()

    if not session_id or not message:
        return JsonResponse({"ok": False, "error": "session_id and message are required"}, status=400)

    active = get_active_workflow(request.user, session_id)

    if active:
        engine = WorkflowEngine(active)

        if is_resume_command(message):
            return JsonResponse({"ok": True, "reply": engine.resume(), "workflow_active": True})

        if is_cancel_command(message):
            return JsonResponse({"ok": True, "reply": engine.cancel(), "workflow_active": False})

        if is_handoff_request(message):
            return JsonResponse({
                "ok": True,
                "reply": engine.request_handoff(message),
                "workflow_active": False,
                "handoff": True,
            })

        change_field = parse_change_command(message)
        if change_field:
            return JsonResponse({"ok": True, "reply": engine.change_answer(change_field), "workflow_active": True})

        result = engine.process_text(message)
        return JsonResponse({
            "ok": result["ok"],
            "reply": result["reply"],
            "workflow_active": not result["completed"],
            "workflow_intent": active.intent_name,
        })

    intent = detect_intent(message)
    if intent == "human_handoff":
        return JsonResponse({
            "ok": True,
            "reply": "Your request will be forwarded to a human agent.",
            "workflow_active": False,
            "handoff": True,
        })

    if intent:
        definition = get_active_definition(intent)
        if not definition:
            return JsonResponse({"ok": False, "error": f"No active workflow definition for {intent}"}, status=400)

        if not user_can_start_workflow(request.user, definition):
            return JsonResponse({"ok": False, "error": "Permission denied for this workflow"}, status=403)

        wf = WorkflowSession.objects.create(
            user=request.user,
            session_id=session_id,
            intent_name=intent,
            definition=definition,
            status="active",
            collected_data={}
        )
        engine = WorkflowEngine(wf)
        return JsonResponse({
            "ok": True,
            "reply": engine.start(),
            "workflow_active": True,
            "workflow_intent": intent,
        })

    return JsonResponse({
        "ok": True,
        "reply": "",
        "workflow_active": False,
        "workflow_intent": None,
    })


@csrf_exempt
@require_POST
def workflow_upload_api(request):
    if not request.user.is_authenticated:
        return JsonResponse({"ok": False, "error": "Authentication required"}, status=401)

    session_id = (request.POST.get("session_id") or "").strip()
    file_type = (request.POST.get("file_type") or "other").strip()
    uploaded_file = request.FILES.get("file")

    if not session_id or not uploaded_file:
        return JsonResponse({"ok": False, "error": "session_id and file are required"}, status=400)

    active = get_active_workflow(request.user, session_id)
    if not active:
        return JsonResponse({"ok": False, "error": "No active workflow found"}, status=404)

    engine = WorkflowEngine(active)
    result = engine.process_file(uploaded_file, file_type)

    return JsonResponse({
        "ok": result["ok"],
        "reply": result["reply"],
        "workflow_active": not result["completed"],
        "workflow_intent": active.intent_name,
    })
