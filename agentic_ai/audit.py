from .models import AgentRunLog


def write_agent_log(
    user=None,
    query="",
    intent=None,
    tool=None,
    tool_input=None,
    tool_output_summary=None,
    final_answer=None,
    status="success",
    risk_level="low",
    error_message=None,
    session_id=None,
):
    try:
        return AgentRunLog.objects.create(
            user=user if getattr(user, "is_authenticated", False) else None,
            user_query=query or "",
            detected_intent=intent,
            selected_tool=tool,
            tool_input=tool_input or {},
            tool_output_summary=tool_output_summary,
            final_answer=final_answer,
            status=status,
            risk_level=risk_level,
            error_message=error_message,
            session_id=session_id,
        )
    except Exception:
        return None
