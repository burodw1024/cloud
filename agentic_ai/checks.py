"""Deployment checks for the private MCP agent."""
import django
from django.conf import settings
from django.core.checks import Error, Tags, Warning, register


@register(Tags.security, deploy=True)
def check_agent_deployment(app_configs, **kwargs):
    from .mcp_gateway import gateway_enabled
    messages = []
    if django.VERSION < (5, 2):
        messages.append(Warning(
            "This Django series is outside its supported security lifetime.",
            hint="Upgrade to an actively supported Django release after compatibility testing.",
            id="agentic_ai.W001",
        ))
    if gateway_enabled():
        backend = settings.CACHES.get("default", {}).get("BACKEND", "")
        if backend.endswith(("LocMemCache", "DummyCache", "FileBasedCache")):
            messages.append(Error(
                "MCP request coordination requires a shared cache with atomic add/incr.",
                hint="Use the shared SQLite cache for this single-node deployment, or Redis for multiple nodes.",
                id="agentic_ai.E001",
            ))
        if settings.DEBUG:
            messages.append(Error("Disable DEBUG before deploying MCP execution.", id="agentic_ai.E002"))
    return messages
