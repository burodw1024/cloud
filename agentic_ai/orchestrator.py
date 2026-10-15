"""Entry point for model-selected tools through the configured MCP gateway."""
from .agent_runtime import run_mcp_query


def run_agentic_query(user=None, query="", session_id=None, history=None):
    return run_mcp_query(user=user, query=query, session_id=session_id, history=history)
