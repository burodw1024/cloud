# chat/views_public.py
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Union

import requests
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

JsonT = Union[Dict[str, Any], List[Any]]

# =========================================================
# Env / Config
# =========================================================
def _env(name: str, default: str) -> str:
    return (os.getenv(name, default) or "").strip()

def _env_int(name: str, default: str) -> int:
    try:
        return int(_env(name, default))
    except Exception:
        return int(default)

def _env_float(name: str, default: str) -> float:
    try:
        return float(_env(name, default))
    except Exception:
        return float(default)

@dataclass(frozen=True)
class Cfg:
    OLLAMA_URL: str = _env("OLLAMA_URL", "http://192.168.1.144:31134").rstrip("/")
    QDRANT_URL: str = _env("QDRANT_URL", "http://192.168.1.176:31633").rstrip("/")

    EMBED_MODEL: str = _env("EMBED_MODEL", "nomic-embed-text:latest")
    EMBED_TIMEOUT: float = _env_float("EMBED_TIMEOUT", "300")
    QDRANT_TIMEOUT: float = _env_float("QDRANT_TIMEOUT", "300")

    # You can keep notices in a separate collection (recommended)
    NOTICE_COLLECTION: str = _env("NOTICE_COLLECTION", "buro_notice").strip() or "buro_notice"

    # If you store notice inside buro_policy, set NOTICE_COLLECTION=buro_policy
    # and keep payload.doc_type="notice".

    TOP_K_NOTICE: int = _env_int("TOP_K_NOTICE", "5")

CFG = Cfg()

ALLOW_ANON_NOTICE = (_env("ALLOW_ANON_NOTICE", "1") or "1").lower() not in ("0", "false", "no")

# =========================================================
# Helpers
# =========================================================
def _json_body(request) -> Dict[str, Any]:
    try:
        raw = (request.body or b"{}").decode("utf-8", errors="replace")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _clean(v: Any) -> str:
    return ("" if v is None else str(v)).strip()

def _http_json(method: str, url: str, *, timeout: float, json_body: Optional[dict] = None) -> JsonT:
    r = requests.request(method, url, json=json_body, timeout=timeout, headers={"Accept": "application/json"})
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code} from {url}: {r.text[:600]}")
    try:
        return r.json()
    except Exception as e:
        raise RuntimeError(f"Invalid JSON from {url}: {e}. Body={r.text[:600]}")

def _ollama_embed(text: str) -> List[float]:
    data = _http_json(
        "POST",
        f"{CFG.OLLAMA_URL}/api/embeddings",
        timeout=CFG.EMBED_TIMEOUT,
        json_body={"model": CFG.EMBED_MODEL, "prompt": text},
    )
    if not isinstance(data, dict) or not isinstance(data.get("embedding"), list):
        raise RuntimeError("Ollama embeddings failed")
    vec = data["embedding"]
    # allow variable sizes if you change model later; but keep check if you want strict:
    if not vec or not isinstance(vec[0], (int, float)):
        raise RuntimeError("Bad embedding vector")
    return vec

def _user_group_names(user) -> Set[str]:
    try:
        if not user or not user.is_authenticated:
            return set()
        return set(user.groups.values_list("name", flat=True))
    except Exception:
        return set()

def _deny_notice(request) -> Optional[JsonResponse]:
    """
    Notice access policy:
    - If ALLOW_ANON_NOTICE=1 => anonymous can use this endpoint
    - Else => requires authentication (but not necessarily staff)
    """
    user = getattr(request, "user", None)
    if (not user or not user.is_authenticated) and not ALLOW_ANON_NOTICE:
        return JsonResponse({"ok": False, "error": "Authentication required."}, status=401)
    return None

# =========================================================
# Rules-based access enforcement (payload-driven)
# =========================================================
def _payload_allows_user(payload: Dict[str, Any], request) -> bool:
    """
    Expected payload fields (recommended):
      - visibility: "public" | "staff" | "groups"
      - allowed_groups: ["DEPT_HR", ...]  (if visibility="groups")
      - allowed_branches: ["001","2092993"] (optional; enforce only if present)
    If fields missing => treat as public notice.
    """
    user = getattr(request, "user", None)
    is_auth = bool(user and getattr(user, "is_authenticated", False))
    is_staff = bool(user and getattr(user, "is_staff", False))
    is_superuser = bool(user and getattr(user, "is_superuser", False))

    if is_superuser:
        return True

    visibility = _clean(payload.get("visibility") or "public").lower()

    if visibility == "public":
        return True

    if visibility == "staff":
        return is_auth and is_staff

    if visibility == "groups":
        if not is_auth:
            return False
        allowed_groups = payload.get("allowed_groups") or []
        allowed_groups = {str(x).strip() for x in allowed_groups if str(x).strip()}
        if not allowed_groups:
            # if misconfigured, default deny for groups-visibility
            return False
        user_groups = _user_group_names(user)
        if not (user_groups & allowed_groups):
            return False

        # Optional branch scoping (only if you implement UserProfile.allowed_branch_ids etc.)
        # If you don't have branch attribute, skip safely.
        allowed_branches = payload.get("allowed_branches") or []
        allowed_branches = {str(x).strip() for x in allowed_branches if str(x).strip()}
        if allowed_branches:
            # try to read user's allowed branches from profile if exists
            prof = getattr(user, "profile", None) or getattr(user, "userprofile", None)
            user_branches = set()
            if prof:
                # adjust according to your model fields
                for attr in ("allowed_branch_ids", "branches", "allowed_branches"):
                    v = getattr(prof, attr, None)
                    if isinstance(v, (list, tuple, set)):
                        user_branches |= {str(x).strip() for x in v if str(x).strip()}
            # if we have an allowed list in payload but cannot determine user branches => deny safe
            if not user_branches:
                return False
            if not (user_branches & allowed_branches):
                return False

        return True

    # Unknown visibility => deny safe
    return False

# =========================================================
# Qdrant search (notice)
# =========================================================
def _qdrant_search_notice(vector: List[float], limit: int) -> List[dict]:
    """
    Requires Qdrant /points/query API (works on recent Qdrant).
    If your Qdrant version differs, we can switch to /points/search.
    """
    body = {
        "query": vector,
        "limit": int(limit),
        "with_payload": True,
        "with_vector": False,
    }

    # If you store notices inside a mixed collection, keep a filter:
    # payload.doc_type == "notice"
    body["filter"] = {
        "must": [
            {"key": "doc_type", "match": {"value": "notice"}}
        ]
    }

    data = _http_json(
        "POST",
        f"{CFG.QDRANT_URL}/collections/{CFG.NOTICE_COLLECTION}/points/query",
        timeout=CFG.QDRANT_TIMEOUT,
        json_body=body,
    )
    res = data.get("result") if isinstance(data, dict) else {}
    pts = (res or {}).get("points") or []
    return pts if isinstance(pts, list) else []

def _format_notice_hit(h: dict) -> dict:
    p = h.get("payload") or {}
    return {
        "id": h.get("id"),
        "score": h.get("score"),
        "title": p.get("title"),
        "source_id": p.get("source_id"),
        "source_file": p.get("source_file"),
        "date": p.get("date"),
        "section": p.get("section"),
        "text_preview": (_clean(p.get("text") or p.get("prompt"))[:220]),
        "visibility": p.get("visibility") or "public",
    }

# =========================================================
# APIs
# =========================================================
@csrf_exempt
@require_POST
def public_notice_search(request):
    """
    POST JSON:
      { "query": "....", "top_k": 5 }

    Returns:
      { ok: true, results: [...] }
    """
    deny = _deny_notice(request)
    if deny:
        return deny

    body = _json_body(request)
    query = _clean(body.get("query") or body.get("question") or body.get("message"))
    if not query:
        return JsonResponse({"ok": False, "error": "query is required"}, status=400)

    top_k = body.get("top_k")
    try:
        top_k = int(top_k) if top_k is not None else CFG.TOP_K_NOTICE
    except Exception:
        top_k = CFG.TOP_K_NOTICE
    if top_k <= 0:
        top_k = CFG.TOP_K_NOTICE
    if top_k > 20:
        top_k = 20

    try:
        # bilingual rewrite helps Bangla/OCR notice text
        retrieval_text = query + " | নোটিশ, বিজ্ঞপ্তি, সার্কুলার"
        vec = _ollama_embed(retrieval_text)
        hits = _qdrant_search_notice(vec, limit=top_k)

        # Apply rules-based filtering
        filtered: List[dict] = []
        for h in hits:
            payload = h.get("payload") or {}
            if _payload_allows_user(payload, request):
                filtered.append(h)

        results = [_format_notice_hit(h) for h in filtered]
        return JsonResponse({"ok": True, "results": results, "count": len(results)})
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Notice search error: {e}"}, status=502)

@csrf_exempt
@require_POST
def public_notice_get(request):
    """
    POST JSON:
      { "id": 123 } OR { "source_id": "notice_2026_01_..." }

    If you want source_id lookup, ensure you store payload.source_id and use scroll.
    """
    deny = _deny_notice(request)
    if deny:
        return deny

    body = _json_body(request)
    pid = body.get("id")
    source_id = _clean(body.get("source_id"))

    try:
        if pid is not None:
            # Direct point get
            data = _http_json(
                "POST",
                f"{CFG.QDRANT_URL}/collections/{CFG.NOTICE_COLLECTION}/points",
                timeout=CFG.QDRANT_TIMEOUT,
                json_body={"ids": [pid], "with_payload": True, "with_vector": False},
            )
            pts = (data.get("result") or []) if isinstance(data, dict) else []
            if not pts:
                return JsonResponse({"ok": False, "error": "Not found."}, status=404)

            p = (pts[0].get("payload") or {})
            if not _payload_allows_user(p, request):
                return JsonResponse({"ok": False, "error": "Access denied."}, status=403)

            return JsonResponse({"ok": True, "notice": pts[0]})

        if source_id:
            # Scroll by payload.source_id
            # Note: Qdrant scroll endpoint:
            data = _http_json(
                "POST",
                f"{CFG.QDRANT_URL}/collections/{CFG.NOTICE_COLLECTION}/points/scroll",
                timeout=CFG.QDRANT_TIMEOUT,
                json_body={
                    "limit": 1,
                    "with_payload": True,
                    "with_vector": False,
                    "filter": {"must": [{"key": "source_id", "match": {"value": source_id}}]},
                },
            )
            pts = ((data.get("result") or {}).get("points") or []) if isinstance(data, dict) else []
            if not pts:
                return JsonResponse({"ok": False, "error": "Not found."}, status=404)

            p = (pts[0].get("payload") or {})
            if not _payload_allows_user(p, request):
                return JsonResponse({"ok": False, "error": "Access denied."}, status=403)

            return JsonResponse({"ok": True, "notice": pts[0]})

        return JsonResponse({"ok": False, "error": "Provide id or source_id"}, status=400)

    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Notice get error: {e}"}, status=502)
