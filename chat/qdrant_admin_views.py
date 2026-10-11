import json
import re
import requests

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render, redirect
from django.views.decorators.csrf import csrf_protect


NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,255}$")


def _qdrant_admin_bases():
    """
    Return allowed Qdrant base URLs for admin dropdown.

    Expected settings.py:

    QDRANT_BASE = "http://192.168.1.176:31633"

    QDRANT_ADMIN_BASES = [
        {"name": "Qdrant Main", "url": "http://192.168.1.176:31633"},
        {"name": "Qdrant Live", "url": "http://10.50.1.29:31635"},
    ]

    QDRANT_ADMIN_DEFAULT_BASE = QDRANT_BASE
    """
    bases = getattr(settings, "QDRANT_ADMIN_BASES", None)

    if not bases:
        default_base = (getattr(settings, "QDRANT_BASE", "") or "").rstrip("/")
        if default_base:
            return [
                {
                    "name": "Default Qdrant",
                    "url": default_base,
                }
            ]
        return []

    clean_bases = []

    for item in bases:
        if not isinstance(item, dict):
            continue

        url = (item.get("url") or "").strip().rstrip("/")
        name = (item.get("name") or url).strip()

        if url:
            clean_bases.append(
                {
                    "name": name,
                    "url": url,
                }
            )

    return clean_bases


def _base_url(request=None) -> str:
    """
    Select Qdrant base URL from GET/POST/session.
    Only URLs from QDRANT_ADMIN_BASES are allowed.
    """
    bases = _qdrant_admin_bases()
    allowed_urls = [item["url"] for item in bases]

    default_base = (
        getattr(settings, "QDRANT_ADMIN_DEFAULT_BASE", None)
        or getattr(settings, "QDRANT_BASE", "")
        or ""
    ).strip().rstrip("/")

    if allowed_urls and default_base not in allowed_urls:
        default_base = allowed_urls[0]

    if request is None:
        return default_base

    selected_base = (
        request.GET.get("base")
        or request.POST.get("base")
        or request.session.get("qdrant_admin_base")
        or default_base
    )

    selected_base = (selected_base or "").strip().rstrip("/")

    if selected_base not in allowed_urls:
        selected_base = default_base

    request.session["qdrant_admin_base"] = selected_base

    return selected_base


def _active_session_key(qbase: str) -> str:
    """
    Keep active collection separate for each Qdrant base.
    This avoids confusion when switching between two Qdrant servers.
    """
    safe_base = (qbase or "").replace(":", "_").replace("/", "_").replace(".", "_")
    return f"qdrant_active_collection_{safe_base}"


def _pretty(obj) -> str:
    try:
        return json.dumps(obj, indent=2, ensure_ascii=False)
    except Exception:
        return str(obj)


def _validate_name(name: str) -> str:
    name = (name or "").strip()

    if not name:
        raise ValueError("Collection name is required.")

    if not NAME_RE.match(name):
        raise ValueError(
            "Invalid name. Allowed: letters, digits, underscore (_), dash (-), dot (.), no spaces."
        )

    return name


def _req(method: str, url: str, *, json_body=None, timeout=20) -> requests.Response:
    try:
        r = requests.request(method, url, json=json_body, timeout=timeout)
    except requests.RequestException as ex:
        raise RuntimeError(f"Qdrant connection error: {ex}") from ex

    if r.status_code >= 300:
        body = (r.text or "")[:1200]
        raise RuntimeError(f"Qdrant HTTP {r.status_code}: {body}")

    return r


def _create_collection(qbase: str, name: str, dims: int, distance: str) -> None:
    """
    Qdrant versions differ, so try both vector config styles.
    """
    payloads = [
        {
            "vectors": {
                "default": {
                    "size": dims,
                    "distance": distance,
                }
            }
        },
        {
            "vectors": {
                "size": dims,
                "distance": distance,
            }
        },
    ]

    last_err = None

    for payload in payloads:
        try:
            _req("PUT", f"{qbase}/collections/{name}", json_body=payload, timeout=25)
            return
        except Exception as ex:
            last_err = ex

    raise last_err or RuntimeError("Create failed.")


def _list_collections(qbase: str):
    r = _req("GET", f"{qbase}/collections", timeout=15)
    data = r.json()
    names = [c["name"] for c in data.get("result", {}).get("collections", [])]
    return names, r.text


def _collection_info(qbase: str, name: str):
    r = _req("GET", f"{qbase}/collections/{name}", timeout=15)
    return r.json(), r.text


def _scroll_points(qbase: str, name: str, limit: int = 20, with_vectors: bool = False):
    """
    POST /collections/{name}/points/scroll
    Keeps with_payload=True so inserted data is visible in admin.
    """
    limit = max(1, min(int(limit), 200))

    payload = {
        "limit": limit,
        "with_payload": True,
        "with_vector": bool(with_vectors),
    }

    r = _req(
        "POST",
        f"{qbase}/collections/{name}/points/scroll",
        json_body=payload,
        timeout=25,
    )

    data = r.json()
    points = data.get("result", {}).get("points", []) or []
    next_page = data.get("result", {}).get("next_page_offset")

    return points, next_page, r.text


@staff_member_required
@csrf_protect
def qdrant_collections_admin(request):
    qbase = _base_url(request)
    qdrant_bases = _qdrant_admin_bases()
    active_key = _active_session_key(qbase)

    msg = None
    err = None

    raw_list = None
    raw_active = None
    raw_selected = None
    raw_points = None

    collections = []
    collection_cards = []

    active = (request.session.get(active_key) or "").strip()
    active_info = None
    selected_info = None

    points = None
    points_next = None

    if not qbase:
        return render(
            request,
            "chat/qdrant_admin.html",
            {
                "qdrant_base": "",
                "qdrant_bases": [],
                "selected_qdrant_base": "",
                "collections": [],
                "collection_cards": [],
                "active_collection": "",
                "active_info_pretty": None,
                "selected_info_pretty": None,
                "raw_list": None,
                "raw_active": None,
                "raw_selected": None,
                "points": None,
                "points_next": None,
                "raw_points": None,
                "msg": None,
                "err": "QDRANT_BASE is not set in settings.py",
            },
        )

    # -------------------------
    # Handle POST actions
    # -------------------------
    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()

        selected = (request.POST.get("selected_collection") or "").strip()
        typed = (request.POST.get("collection_name") or "").strip()
        confirm = (request.POST.get("confirm_name") or "").strip()
        name = typed or selected

        try:
            dims = int(request.POST.get("dims") or "768")
        except Exception:
            dims = 768

        distance = (request.POST.get("distance") or "Cosine").strip() or "Cosine"

        try:
            if action in (
                "create",
                "delete",
                "recreate",
                "set_active",
                "info",
                "list_points",
            ):
                name = _validate_name(name)

            if action in ("delete", "recreate") and confirm != name:
                raise ValueError(
                    "Confirm name must exactly match the target collection name."
                )

            if action == "create":
                _create_collection(qbase, name, dims, distance)
                request.session["qdrant_admin_msg"] = f"Created collection: {name}"
                return redirect("qdrant_collections_admin")

            if action == "delete":
                _req("DELETE", f"{qbase}/collections/{name}", timeout=25)

                if active == name:
                    request.session[active_key] = ""

                request.session["qdrant_admin_msg"] = f"Deleted collection: {name}"
                return redirect("qdrant_collections_admin")

            if action == "recreate":
                try:
                    _req("DELETE", f"{qbase}/collections/{name}", timeout=25)
                except Exception as ex:
                    if " 404" not in str(ex):
                        raise

                _create_collection(qbase, name, dims, distance)
                request.session["qdrant_admin_msg"] = f"Recreated collection: {name}"
                return redirect("qdrant_collections_admin")

            if action == "set_active":
                request.session[active_key] = name
                request.session["qdrant_admin_msg"] = f"Active collection set: {name}"
                return redirect("qdrant_collections_admin")

            if action == "info":
                selected_info, raw_selected = _collection_info(qbase, name)
                msg = f"Info loaded: {name}"

            if action == "list_points":
                limit = int(request.POST.get("points_limit") or "20")
                with_vectors = bool(request.POST.get("with_vectors"))
                points, points_next, raw_points = _scroll_points(
                    qbase,
                    name,
                    limit=limit,
                    with_vectors=with_vectors,
                )
                msg = f"Showing {len(points)} points from: {name}"

            if action not in (
                "info",
                "list_points",
                "create",
                "delete",
                "recreate",
                "set_active",
            ):
                raise ValueError("Unknown action.")

        except Exception as ex:
            err = str(ex)

    # Show success message after redirect
    if not msg:
        msg = request.session.pop("qdrant_admin_msg", None)

    # -------------------------
    # Fetch list of collections
    # -------------------------
    try:
        collections, raw_list = _list_collections(qbase)
    except Exception as ex:
        err = err or str(ex)

    # -------------------------
    # Build collection summary cards
    # -------------------------
    for name in collections:
        card = {
            "name": name,
            "points": None,
            "vectors": None,
            "distance": None,
        }

        try:
            info, _raw = _collection_info(qbase, name)
            res = info.get("result", {})

            card["points"] = res.get("points_count")

            vectors = (
                (res.get("config", {}) or {})
                .get("params", {})
                .get("vectors", {})
            )

            if isinstance(vectors, dict):
                if "size" in vectors:
                    card["vectors"] = vectors.get("size")
                    card["distance"] = vectors.get("distance")
                elif "default" in vectors and isinstance(vectors["default"], dict):
                    card["vectors"] = vectors["default"].get("size")
                    card["distance"] = vectors["default"].get("distance")

        except Exception:
            pass

        collection_cards.append(card)

    # -------------------------
    # Active collection info
    # -------------------------
    active = (request.session.get(active_key) or "").strip()

    if active:
        try:
            active_info, raw_active = _collection_info(qbase, active)
        except Exception as ex:
            active_info = {"error": str(ex)}

    return render(
        request,
        "chat/qdrant_admin.html",
        {
            "qdrant_base": qbase,
            "qdrant_bases": qdrant_bases,
            "selected_qdrant_base": qbase,
            "collections": collections,
            "collection_cards": collection_cards,
            "active_collection": active,
            "active_info_pretty": _pretty(active_info) if active_info else None,
            "selected_info_pretty": _pretty(selected_info) if selected_info else None,
            "raw_list": raw_list,
            "raw_active": raw_active,
            "raw_selected": raw_selected,
            "points": points,
            "points_next": points_next,
            "raw_points": raw_points,
            "msg": msg,
            "err": err,
        },
    )
