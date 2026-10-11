import json
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render

from .orchestrator import run_agentic_query
from .models import AgentRunLog, AgentApprovalRequest


@csrf_exempt
def agentic_query_api(request):
    """
    POST /agentic/api/query/
    Body:
    {
      "message": "your question"
    }
    """
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required"}, status=405)

    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        body = request.POST

    query = body.get("message") or body.get("query") or ""

    if not query.strip():
        return JsonResponse({"ok": False, "error": "message/query is required"}, status=400)

    result = run_agentic_query(
        user=request.user,
        query=query,
        session_id=request.session.session_key,
    )

    return JsonResponse(result, safe=False)


@staff_member_required
def dashboard(request):
    logs = AgentRunLog.objects.all()[:50]
    approvals = AgentApprovalRequest.objects.filter(status="pending")[:50]
    return render(
        request,
        "agentic_ai/dashboard.html",
        {
            "logs": logs,
            "approvals": approvals,
        },
    )


@staff_member_required
def logs_api(request):
    logs = AgentRunLog.objects.all()[:100]
    data = [
        {
            "id": x.id,
            "created_at": x.created_at.isoformat(),
            "user": x.user.username if x.user else None,
            "query": x.user_query,
            "intent": x.detected_intent,
            "tool": x.selected_tool,
            "status": x.status,
            "risk_level": x.risk_level,
        }
        for x in logs
    ]
    return JsonResponse({"ok": True, "logs": data})


@csrf_exempt
def public_website_query_api(request):
    """
    Public no-login endpoint for BURO website RAG only.

    POST /agentic/api/website-query/
    Body:
    {
      "message": "বুরো বাংলাদেশ কী কী সেবা প্রদান করে?"
    }

    Important:
    This endpoint must NOT call full agentic orchestrator.
    It only calls website_rag_tool, so public users cannot access NiFi/HR/email/Teams.
    """
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required"}, status=405)

    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        body = request.POST

    query = body.get("message") or body.get("query") or ""

    if not query.strip():
        return JsonResponse(
            {"ok": False, "error": "message/query is required"},
            status=400,
        )

    try:
        from agentic_ai.tools.website_rag_tool import run as run_website_rag
        from agentic_ai.audit import write_agent_log

        # Simple language detection
        language = "bn" if any("\u0980" <= ch <= "\u09FF" for ch in query) else "en"

        result = run_website_rag(
            user=None,
            query=query,
            language=language,
            params={"limit": 5},
        )

        write_agent_log(
            user=None,
            query=query,
            intent="public_website_rag",
            tool="website_rag_tool",
            tool_input={"query": query},
            tool_output_summary=str(result)[:2000],
            final_answer=str(result.get("answer", ""))[:5000],
            status="success" if result.get("ok") else "failed",
            risk_level="low",
            session_id=request.session.session_key,
        )

        return JsonResponse({
            "ok": result.get("ok", False),
            "intent": "public_website_rag",
            "tool": "website_rag_tool",
            "answer": result.get("answer"),
            "sources": result.get("sources", []),
            "matches_count": result.get("matches_count", 0),
            "source_collection": result.get("source"),
        }, safe=False)

    except Exception as exc:
        return JsonResponse({
            "ok": False,
            "intent": "public_website_rag",
            "tool": "website_rag_tool",
            "answer": f"Website RAG error: {exc}",
            "error": str(exc),
        }, status=500)


@csrf_exempt
def public_website_query_api(request):
    """
    Public no-login endpoint for BURO website RAG only.

    POST /agentic/api/website-query/
    Body:
    {
      "message": "বুরো বাংলাদেশ কী কী সেবা প্রদান করে?"
    }

    Important:
    This endpoint must NOT call full agentic orchestrator.
    It only calls website_rag_tool, so public users cannot access NiFi/HR/email/Teams.
    """
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required"}, status=405)

    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        body = request.POST

    query = body.get("message") or body.get("query") or ""

    if not query.strip():
        return JsonResponse(
            {"ok": False, "error": "message/query is required"},
            status=400,
        )

    try:
        from agentic_ai.tools.website_rag_tool import run as run_website_rag
        from agentic_ai.audit import write_agent_log

        # Simple language detection
        language = "bn" if any("\u0980" <= ch <= "\u09FF" for ch in query) else "en"

        result = run_website_rag(
            user=None,
            query=query,
            language=language,
            params={"limit": 5},
        )

        write_agent_log(
            user=None,
            query=query,
            intent="public_website_rag",
            tool="website_rag_tool",
            tool_input={"query": query},
            tool_output_summary=str(result)[:2000],
            final_answer=str(result.get("answer", ""))[:5000],
            status="success" if result.get("ok") else "failed",
            risk_level="low",
            session_id=request.session.session_key,
        )

        return JsonResponse({
            "ok": result.get("ok", False),
            "intent": "public_website_rag",
            "tool": "website_rag_tool",
            "answer": result.get("answer"),
            "sources": result.get("sources", []),
            "matches_count": result.get("matches_count", 0),
            "source_collection": result.get("source"),
        }, safe=False)

    except Exception as exc:
        return JsonResponse({
            "ok": False,
            "intent": "public_website_rag",
            "tool": "website_rag_tool",
            "answer": f"Website RAG error: {exc}",
            "error": str(exc),
        }, status=500)


@csrf_exempt
def public_website_query_api(request):
    """
    Public no-login endpoint for BURO website RAG only.

    POST /agentic/api/website-query/
    Body:
    {
      "message": "বুরো বাংলাদেশ কী কী সেবা প্রদান করে?"
    }

    Important:
    This endpoint must NOT call full agentic orchestrator.
    It only calls website_rag_tool, so public users cannot access NiFi/HR/email/Teams.
    """
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required"}, status=405)

    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        body = request.POST

    query = body.get("message") or body.get("query") or ""

    if not query.strip():
        return JsonResponse(
            {"ok": False, "error": "message/query is required"},
            status=400,
        )

    try:
        from agentic_ai.tools.website_rag_tool import run as run_website_rag
        from agentic_ai.audit import write_agent_log

        # Simple language detection
        language = "bn" if any("\u0980" <= ch <= "\u09FF" for ch in query) else "en"

        result = run_website_rag(
            user=None,
            query=query,
            language=language,
            params={"limit": 5},
        )

        write_agent_log(
            user=None,
            query=query,
            intent="public_website_rag",
            tool="website_rag_tool",
            tool_input={"query": query},
            tool_output_summary=str(result)[:2000],
            final_answer=str(result.get("answer", ""))[:5000],
            status="success" if result.get("ok") else "failed",
            risk_level="low",
            session_id=request.session.session_key,
        )

        return JsonResponse({
            "ok": result.get("ok", False),
            "intent": "public_website_rag",
            "tool": "website_rag_tool",
            "answer": result.get("answer"),
            "sources": result.get("sources", []),
            "matches_count": result.get("matches_count", 0),
            "source_collection": result.get("source"),
        }, safe=False)

    except Exception as exc:
        return JsonResponse({
            "ok": False,
            "intent": "public_website_rag",
            "tool": "website_rag_tool",
            "answer": f"Website RAG error: {exc}",
            "error": str(exc),
        }, status=500)
