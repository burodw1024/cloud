from __future__ import annotations

from typing import Set

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponseForbidden
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from agentic_ai.tools.website_rag_tool import sync_buro_website


ALLOWED_GROUPS = {
    "admin",
    "super_admin",
    "support_manager",
    "buro_website_sync_admin",
}


def user_role_names(user) -> Set[str]:
    roles = set()

    try:
        roles.update(g.name.lower() for g in user.groups.all())
    except Exception:
        pass

    try:
        role = getattr(user, "role", None)
        if role:
            roles.add(str(role).lower())
    except Exception:
        pass

    try:
        profile = getattr(user, "profile", None)
        role = getattr(profile, "role", None)
        if role:
            roles.add(str(getattr(role, "name", role)).lower())
    except Exception:
        pass

    return roles


def can_sync(user) -> bool:
    if not user or not user.is_authenticated:
        return False

    if user.is_superuser:
        return True

    roles = user_role_names(user)

    if roles.intersection(ALLOWED_GROUPS):
        return True

    return False


@login_required
@require_http_methods(["GET", "POST"])
def website_sync_page(request: HttpRequest):
    if not can_sync(request.user):
        return HttpResponseForbidden("You do not have permission to run Website Clean Sync.")

    context = {
        "result": None,
        "error": None,
        "max_pages": 80,
        "recreate": True,
        "roles": sorted(user_role_names(request.user)),
    }

    if request.method == "GET":
        return render(request, "admin/website_sync.html", context)

    try:
        max_pages = int(request.POST.get("max_pages") or 80)
        recreate = request.POST.get("recreate") == "on"

        if max_pages < 1:
            max_pages = 1
        if max_pages > 300:
            max_pages = 300

        result = sync_buro_website(max_pages=max_pages, recreate_collection=recreate)

        context["result"] = result
        context["max_pages"] = max_pages
        context["recreate"] = recreate

        return render(request, "admin/website_sync.html", context)

    except Exception as exc:
        context["error"] = str(exc)
        return render(request, "admin/website_sync.html", context)
