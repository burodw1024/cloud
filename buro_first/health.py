"""Small Kubernetes probes; report status without exposing connection details."""
import os

from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.http import require_safe


@require_safe
def live(request):
    response = JsonResponse({"ok": True})
    response["Cache-Control"] = "no-store"
    return response


@require_safe
def ready(request):
    healthy = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            healthy = cursor.fetchone()[0] == 1
        key = "buro:health:" + os.environ.get("HOSTNAME", "web")
        cache.set(key, "ready", 30)
        healthy = healthy and cache.get(key) == "ready"
    except Exception:
        healthy = False
    response = JsonResponse({"ok": healthy}, status=200 if healthy else 503)
    response["Cache-Control"] = "no-store"
    return response
