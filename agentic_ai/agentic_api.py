import json
import logging

from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods, require_POST

from .agent_runtime import resume_mcp_query
from .models import AgentApprovalRequest
from .audit import write_agent_log
from .orchestrator import run_agentic_query

logger = logging.getLogger(__name__)


def _json(data, status=200):
    return JsonResponse(data, status=status, json_dumps_params={"ensure_ascii": False})


def _authenticated(request):
    return request.user.is_authenticated and request.user.is_active


def _reviewer(user):
    return user.is_active and user.is_staff and user.has_perm("agentic_ai.change_agentapprovalrequest")


def _remember(request, query, result):
    if result.get("ok") and isinstance(result.get("answer"), str):
        history = request.session.get("mcp_agent_history", [])
        history.extend([{"role": "user", "content": query[:6000]}, {"role": "assistant", "content": result["answer"][:6000]}])
        request.session["mcp_agent_history"] = history[-10:]


@require_POST
@csrf_protect
def query_api(request):
    if not _authenticated(request):
        return _json({"ok": False, "answer": "Log in to use Agentic AI.", "login_required": True}, 401)
    if len(request.body) > 40000:
        return _json({"ok": False, "answer": "The request is too large."}, 413)
    try:
        body = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        return _json({"ok": False, "answer": "Send a valid JSON object."}, 400)
    if not isinstance(body, dict):
        return _json({"ok": False, "answer": "Send a JSON object."}, 400)
    query = body.get("message", body.get("query", ""))
    if not isinstance(query, str) or not query.strip() or len(query) > 12000:
        return _json({"ok": False, "answer": "Enter a question between 1 and 12,000 characters."}, 400)
    if body.get("file_id"):
        return _json({"ok": False, "answer": "Agentic file analysis is not connected yet. Clear the file or choose a document module."}, 400)
    if body.get("reset_history") is True:
        request.session.pop("mcp_agent_history", None)
    if not request.session.session_key:
        request.session.create()
    result = run_agentic_query(user=request.user, query=query.strip(), session_id=request.session.session_key,
                               history=request.session.get("mcp_agent_history", []))
    _remember(request, query.strip(), result)
    # Business failures and approval pauses remain JSON results; authentication
    # and invalid input use explicit HTTP statuses.
    return _json(result)


@require_http_methods(["GET", "POST"])
@csrf_protect
def approval_api(request, approval_id):
    if not _authenticated(request):
        return _json({"ok": False, "answer": "Log in to view approvals."}, 401)
    reviewer = _reviewer(request.user)
    if request.method == "GET":
        approval = AgentApprovalRequest.objects.filter(pk=approval_id).first()
        if not approval or (approval.user_id != request.user.pk and not reviewer):
            return _json({"ok": False, "answer": "Approval not found."}, 404)
        if approval.action_type != "mcp_tool_call":
            return _json({"ok": False, "answer": "This request belongs to the legacy approval workflow."}, 400)
        data = {"ok": True, "approval_id": approval.pk, "status": approval.status,
                "tool": approval.tool_name, "arguments": approval.request_payload.get("arguments", {})}
        if approval.response_note:
            try:
                data["result"] = json.loads(approval.response_note)
            except ValueError:
                pass
        if approval.user_id == request.user.pk and data.get("result", {}).get("ok"):
            remembered = request.session.get("mcp_remembered_approvals", [])
            if approval.pk not in remembered:
                _remember(request, approval.request_payload.get("state", {}).get("query", ""), data["result"])
                request.session["mcp_remembered_approvals"] = (remembered + [approval.pk])[-50:]
        return _json(data)
    if not reviewer:
        return _json({"ok": False, "answer": "You need staff access and approval review permission."}, 403)
    try:
        if len(request.body) > 1000:
            raise ValueError()
        body = json.loads(request.body)
        decision = body.get("decision") if isinstance(body, dict) else None
        if decision not in {"approve", "reject"}:
            raise ValueError()
    except (ValueError, UnicodeDecodeError):
        return _json({"ok": False, "answer": "Choose approve or reject."}, 400)
    # Claim before calling the external service. A second review never repeats
    # a mutation. A crashed execution must be reviewed manually, not retried.
    with transaction.atomic():
        approval = AgentApprovalRequest.objects.select_for_update().filter(pk=approval_id).first()
        if not approval:
            return _json({"ok": False, "answer": "Approval not found."}, 404)
        if approval.action_type != "mcp_tool_call" or approval.request_payload.get("version") != 1:
            return _json({"ok": False, "answer": "This is not an MCP approval request."}, 400)
        if approval.status != "pending":
            return _json({"ok": False, "answer": "This request was already reviewed. Refresh its status."}, 409)
        approval.status = "approved" if decision == "approve" else "rejected"
        approval.approved_by = request.user
        approval.response_note = json.dumps({"ok": False, "status": "executing" if decision == "approve" else "rejected",
                                             "answer": "Approved action is running." if decision == "approve" else "The action was rejected. No pending tool was executed."})
        approval.save(update_fields=["status", "approved_by", "response_note", "updated_at"])
    if decision == "reject":
        write_agent_log(user=approval.user, query=approval.request_payload.get("state", {}).get("query", ""),
                        intent="mcp_approval_rejected", tool=approval.tool_name,
                        tool_input={"approval_id": approval.pk, "reviewer_id": request.user.pk},
                        final_answer="The requested action was rejected.", status="blocked", risk_level="high")
        return _json({"ok": True, "status": "rejected", "approval_id": approval.pk})
    try:
        result = resume_mcp_query(approval)
    except Exception:
        logger.exception("MCP approval execution failed for approval %s", approval.pk)
        result = {"ok": False, "status": "failed", "answer": "Execution could not be confirmed. An administrator must check the tool's result before retrying."}
    approval.response_note = json.dumps(result, ensure_ascii=False)
    approval.save(update_fields=["response_note", "updated_at"])
    return _json({"ok": True, "status": "approved", "approval_id": approval.pk, "result": result})
