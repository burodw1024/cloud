from .planner import plan_query
from .tool_registry import TOOL_REGISTRY
from .permissions import has_tool_permission
from .audit import write_agent_log
from .models import AgentApprovalRequest


def run_agentic_query(user=None, query="", session_id=None):
    from . import mcp_agent
    if mcp_agent.enabled():
        return mcp_agent.run(user, query, session_id)

    plan = plan_query(query)
    tool_name = plan.get("tool")
    tool_config = TOOL_REGISTRY.get(tool_name)

    if not tool_config:
        answer = f"No registered tool found for: {tool_name}"
        write_agent_log(
            user=user,
            query=query,
            intent=plan.get("intent"),
            tool=tool_name,
            final_answer=answer,
            status="failed",
            risk_level=plan.get("risk_level", "low"),
            session_id=session_id,
        )
        return {
            "ok": False,
            "intent": plan.get("intent"),
            "tool": tool_name,
            "answer": answer,
            "plan": plan,
        }

    if not has_tool_permission(user, tool_config):
        answer = "Permission denied for this agentic tool."
        write_agent_log(
            user=user,
            query=query,
            intent=plan.get("intent"),
            tool=tool_name,
            final_answer=answer,
            status="blocked",
            risk_level=plan.get("risk_level", "medium"),
            session_id=session_id,
        )
        return {
            "ok": False,
            "intent": plan.get("intent"),
            "tool": tool_name,
            "answer": answer,
            "plan": plan,
        }

    requires_approval = bool(
        plan.get("requires_approval") or tool_config.get("requires_approval")
    )

    if requires_approval:
        approval = AgentApprovalRequest.objects.create(
            user=user if getattr(user, "is_authenticated", False) else None,
            action_type=plan.get("intent", "unknown"),
            tool_name=tool_name,
            request_payload={
                "query": query,
                "plan": plan,
            },
        )

        answer = f"Approval required before executing tool: {tool_name}. Approval ID: {approval.id}"

        write_agent_log(
            user=user,
            query=query,
            intent=plan.get("intent"),
            tool=tool_name,
            tool_input=plan.get("params", {}),
            final_answer=answer,
            status="approval_required",
            risk_level=plan.get("risk_level", "high"),
            session_id=session_id,
        )

        return {
            "ok": False,
            "approval_required": True,
            "approval_id": approval.id,
            "intent": plan.get("intent"),
            "tool": tool_name,
            "answer": answer,
            "plan": plan,
        }

    try:
        tool_function = tool_config["function"]
        result = tool_function(
            user=user,
            query=query,
            params=plan.get("params", {}),
            language=plan.get("language", "en"),
            plan=plan,
        )

        final_answer = result.get("answer", result)

        write_agent_log(
            user=user,
            query=query,
            intent=plan.get("intent"),
            tool=tool_name,
            tool_input=plan.get("params", {}),
            tool_output_summary=str(result)[:2000],
            final_answer=str(final_answer)[:5000],
            status="success" if result.get("ok", False) else "failed",
            risk_level=plan.get("risk_level", "low"),
            session_id=session_id,
        )

        return {
            "ok": result.get("ok", False),
            "intent": plan.get("intent"),
            "tool": tool_name,
            "answer": final_answer,
            "raw": result,
            "plan": plan,
        }

    except Exception as exc:
        answer = f"Agentic execution error: {exc}"

        write_agent_log(
            user=user,
            query=query,
            intent=plan.get("intent"),
            tool=tool_name,
            tool_input=plan.get("params", {}),
            final_answer=answer,
            status="failed",
            risk_level=plan.get("risk_level", "medium"),
            error_message=str(exc),
            session_id=session_id,
        )

        return {
            "ok": False,
            "intent": plan.get("intent"),
            "tool": tool_name,
            "answer": answer,
            "plan": plan,
        }
