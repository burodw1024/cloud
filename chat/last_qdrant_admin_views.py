import json
import re
import requests

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render, redirect
from django.views.decorators.csrf import csrf_protect


NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,255}$")


def _base_url() -> str:
    return (getattr(settings, "QDRANT_BASE", "") or "").rstrip("/")


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
        raise ValueError("Invalid name. Allowed: letters, digits, underscore (_), dash (-), dot (.), no spaces.")
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
    # Qdrant versions differ; try both payload styles
    payloads = [
        {"vectors": {"default": {"size": dims, "distance": distance}}},
        {"vectors": {"size": dims, "distance": distance}},
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
    # POST /collections/{name}/points/scroll
    # We keep with_payload True so you can see inserted data.
    limit = max(1, min(int(limit), 200))  # cap to 200
    payload = {
        "limit": limit,
        "with_payload": True,
        "with_vector": bool(with_vectors),
    }
    r = _req("POST", f"{qbase}/collections/{name}/points/scroll", json_body=payload, timeout=25)
    data = r.json()
    points = data.get("result", {}).get("points", []) or []
    next_page = data.get("result", {}).get("next_page_offset")
    return points, next_page, r.text


@staff_member_required
@csrf_protect
def qdrant_collections_admin(request):
    qbase = _base_url()

    msg = None
    err = None

    raw_list = None
    raw_active = None
    raw_selected = None
    raw_points = None

    collections = []
    collection_cards = []  # name + points + vector config (summary)

    active = (request.session.get("qdrant_active_collection") or "").strip()
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

        # defaults for create/recreate
        try:
            dims = int(request.POST.get("dims") or "768")
        except Exception:
            dims = 768

        distance = (request.POST.get("distance") or "Cosine").strip() or "Cosine"

        try:
            # Actions that require a name
            if action in ("create", "delete", "recreate", "set_active", "info", "list_points"):
                name = _validate_name(name)

            if action in ("delete", "recreate") and confirm != name:
                raise ValueError("Confirm name must exactly match the target collection name.")

            if action == "create":
                _create_collection(qbase, name, dims, distance)
                msg = f"Created collection: {name}"
                return redirect("qdrant_collections_admin")

            if action == "delete":
                _req("DELETE", f"{qbase}/collections/{name}", timeout=25)
                msg = f"Deleted collection: {name}"
                if active == name:
                    request.session["qdrant_active_collection"] = ""
                return redirect("qdrant_collections_admin")

            if action == "recreate":
                # delete (ignore 404)
                try:
                    _req("DELETE", f"{qbase}/collections/{name}", timeout=25)
                except Exception as ex:
                    if " 404" not in str(ex):
                        raise
                _create_collection(qbase, name, dims, distance)
                msg = f"Recreated collection: {name}"
                return redirect("qdrant_collections_admin")

            if action == "set_active":
                request.session["qdrant_active_collection"] = name
                msg = f"Active collection set: {name}"
                return redirect("qdrant_collections_admin")

            if action == "info":
                selected_info, raw_selected = _collection_info(qbase, name)
                msg = f"Info loaded: {name}"

            if action == "list_points":
                limit = int(request.POST.get("points_limit") or "20")
                with_vectors = bool(request.POST.get("with_vectors"))
                points, points_next, raw_points = _scroll_points(qbase, name, limit=limit, with_vectors=with_vectors)
                msg = f"Showing {len(points)} points from: {name}"

            if action not in ("info", "list_points", "create", "delete", "recreate", "set_active"):
                raise ValueError("Unknown action.")

        except Exception as ex:
            err = str(ex)

    # -------------------------
    # Fetch list of collections
    # -------------------------
    try:
        collections, raw_list = _list_collections(qbase)
    except Exception as ex:
        err = err or str(ex)

    # Build summary cards (safe + useful)
    for name in collections:
        card = {"name": name, "points": None, "vectors": None, "distance": None}
        try:
            info, _raw = _collection_info(qbase, name)
            res = info.get("result", {})
            card["points"] = res.get("points_count")
            vectors = (res.get("config", {}) or {}).get("params", {}).get("vectors", {})
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
    active = (request.session.get("qdrant_active_collection") or "").strip()
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
