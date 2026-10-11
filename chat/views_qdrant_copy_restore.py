from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Set

import requests
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponseForbidden
from django.shortcuts import render
from django.views.decorators.http import require_http_methods


DEFAULT_SRC = os.getenv("QDRANT_COPY_DEFAULT_SRC", "http://192.168.1.176:31633")
DEFAULT_DST = os.getenv("QDRANT_COPY_DEFAULT_DST", "http://10.50.1.29:31635")
DEFAULT_PRESERVE = os.getenv("QDRANT_COPY_DEFAULT_PRESERVE", "customer_support")
DEFAULT_WORK_ROOT = os.getenv(
    "QDRANT_COPY_WORK_ROOT",
    "/home/mamun/finalapp/qdrant-copy-restore-backups",
)

TIMEOUT = int(os.getenv("QDRANT_COPY_TIMEOUT", "120"))


ALLOWED_GROUPS = {
    "admin",
    "super_admin",
    "support_manager",
    "qdrant_admin",
    "qdrant_copy_restore_admin",
}


def _clean_url(url: str) -> str:
    return (url or "").strip().rstrip("/")


def _safe_name(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in (name or ""))


def _json_or_text(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return {"raw": resp.text}


def _user_role_names(user) -> Set[str]:
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
        role_name = getattr(user, "role_name", None)
        if role_name:
            roles.add(str(role_name).lower())
    except Exception:
        pass

    try:
        profile = getattr(user, "profile", None)
        role = getattr(profile, "role", None)
        if role:
            roles.add(str(getattr(role, "name", role)).lower())
    except Exception:
        pass

    try:
        profile = getattr(user, "profile", None)
        role_name = getattr(profile, "role_name", None)
        if role_name:
            roles.add(str(role_name).lower())
    except Exception:
        pass

    return roles


def _can_run_qdrant_copy_restore(user) -> bool:
    if not user or not user.is_authenticated:
        return False

    if getattr(user, "is_superuser", False):
        return True

    return bool(_user_role_names(user).intersection(ALLOWED_GROUPS))


def _request_headers() -> Dict[str, str]:
    headers = {"Accept": "application/json"}

    api_key = os.getenv("QDRANT_API_KEY", "").strip()
    if api_key:
        headers["api-key"] = api_key

    return headers


def qdrant_get_json(base_url: str, path: str) -> Dict[str, Any]:
    resp = requests.get(
        f"{base_url}{path}",
        headers=_request_headers(),
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    data = _json_or_text(resp)
    return data if isinstance(data, dict) else {"data": data}


def qdrant_post_json(base_url: str, path: str, payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
    resp = requests.post(
        f"{base_url}{path}",
        json=payload,
        headers=_request_headers(),
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    data = _json_or_text(resp)
    return data if isinstance(data, dict) else {"data": data}


def qdrant_delete_json(base_url: str, path: str) -> Dict[str, Any]:
    resp = requests.delete(
        f"{base_url}{path}",
        headers=_request_headers(),
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    data = _json_or_text(resp)
    return data if isinstance(data, dict) else {"data": data}


def list_collections(base_url: str) -> List[str]:
    data = qdrant_get_json(base_url, "/collections")
    return [
        c.get("name")
        for c in data.get("result", {}).get("collections", [])
        if c.get("name")
    ]


def collection_exists(base_url: str, collection: str) -> bool:
    try:
        requests.get(
            f"{base_url}/collections/{collection}",
            headers=_request_headers(),
            timeout=TIMEOUT,
        ).raise_for_status()
        return True
    except Exception:
        return False


def point_count(base_url: str, collection: str) -> Any:
    try:
        data = qdrant_get_json(base_url, f"/collections/{collection}")
        return data.get("result", {}).get("points_count")
    except Exception:
        return None


def create_snapshot(base_url: str, collection: str) -> str:
    data = qdrant_post_json(base_url, f"/collections/{collection}/snapshots?wait=true")
    snapshot_name = data.get("result", {}).get("name")

    if not snapshot_name:
        raise RuntimeError(f"Snapshot creation failed for {collection}: {data}")

    return snapshot_name


def download_snapshot(base_url: str, collection: str, snapshot_name: str, out_file: Path) -> None:
    resp = requests.get(
        f"{base_url}/collections/{collection}/snapshots/{snapshot_name}",
        headers=_request_headers(),
        timeout=TIMEOUT,
        stream=True,
    )
    resp.raise_for_status()

    with out_file.open("wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)


def upload_snapshot(base_url: str, collection: str, snapshot_file: Path) -> Dict[str, Any]:
    with snapshot_file.open("rb") as f:
        resp = requests.post(
            f"{base_url}/collections/{collection}/snapshots/upload?priority=snapshot&wait=true",
            headers=_request_headers(),
            files={"snapshot": (snapshot_file.name, f, "application/octet-stream")},
            timeout=TIMEOUT,
        )

    resp.raise_for_status()
    data = _json_or_text(resp)
    return data if isinstance(data, dict) else {"data": data}


def get_aliases(base_url: str) -> List[Dict[str, Any]]:
    try:
        data = qdrant_get_json(base_url, "/aliases")
        return data.get("result", {}).get("aliases", []) or []
    except Exception:
        return []


def create_alias(base_url: str, alias_name: str, collection_name: str) -> Dict[str, Any]:
    payload = {
        "actions": [
            {
                "create_alias": {
                    "collection_name": collection_name,
                    "alias_name": alias_name,
                }
            }
        ]
    }

    return qdrant_post_json(base_url, "/collections/aliases", payload)


def parse_preserve_collections(raw: str) -> Set[str]:
    return {
        item.strip()
        for item in (raw or "").split(",")
        if item.strip()
    }


def run_qdrant_copy_restore(
    *,
    src_url: str,
    dst_url: str,
    collection_mode: str,
    collection_name: str,
    preserve_collections: Set[str],
    delete_before_restore: bool,
    backup_destination: bool,
    restore_aliases: bool,
    dry_run: bool,
) -> Dict[str, Any]:
    src_url = _clean_url(src_url)
    dst_url = _clean_url(dst_url)

    if not src_url or not dst_url:
        raise ValueError("Source and destination Qdrant URLs are required.")

    if collection_mode not in {"single", "all"}:
        raise ValueError("Invalid collection mode. Use single or all.")

    if collection_mode == "single" and not collection_name:
        raise ValueError("Collection name is required for single collection mode.")

    started = time.time()
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    work_root = Path(DEFAULT_WORK_ROOT)
    work_dir = work_root / f"qdrant-copy-restore-{timestamp}"
    src_backup_dir = work_dir / "source"
    dst_backup_dir = work_dir / "destination-before-copy"

    src_backup_dir.mkdir(parents=True, exist_ok=True)
    dst_backup_dir.mkdir(parents=True, exist_ok=True)

    log: List[str] = []
    verification: List[Dict[str, Any]] = []

    log.append(f"Work directory: {work_dir}")
    log.append(f"Source: {src_url}")
    log.append(f"Destination: {dst_url}")

    src_collections = list_collections(src_url)
    dst_collections = list_collections(dst_url)

    if collection_mode == "single":
        collections_to_restore = [collection_name]
    else:
        collections_to_restore = src_collections

    collections_to_restore = [
        c for c in collections_to_restore
        if c and c not in preserve_collections
    ]

    if dry_run:
        return {
            "status": "dry_run",
            "source_url": src_url,
            "destination_url": dst_url,
            "source_collections": src_collections,
            "destination_collections": dst_collections,
            "collections_to_restore": collections_to_restore,
            "preserve_collections": sorted(preserve_collections),
            "delete_before_restore": delete_before_restore,
            "backup_destination": backup_destination,
            "restore_aliases": restore_aliases,
            "work_dir": str(work_dir),
            "log": log,
        }

    # 1. Backup destination
    if backup_destination:
        log.append("Backing up destination collections...")

        if collection_mode == "single":
            dst_backup_list = [collection_name] if collection_name in dst_collections else []
        else:
            dst_backup_list = dst_collections

        for collection in dst_backup_list:
            try:
                snap = create_snapshot(dst_url, collection)
                out_file = dst_backup_dir / f"{_safe_name(collection)}.snapshot"
                download_snapshot(dst_url, collection, snap, out_file)
                log.append(f"Destination backup created: {collection} -> {out_file}")
            except Exception as exc:
                log.append(f"Destination backup failed for {collection}: {exc}")

    # 2. Backup source selected collections
    log.append("Creating source snapshots...")

    source_snapshots: Dict[str, str] = {}

    for collection in collections_to_restore:
        if collection not in src_collections:
            raise RuntimeError(f"Source collection not found: {collection}")

        snap = create_snapshot(src_url, collection)
        out_file = src_backup_dir / f"{_safe_name(collection)}.snapshot"
        download_snapshot(src_url, collection, snap, out_file)
        source_snapshots[collection] = str(out_file)
        log.append(f"Source snapshot created: {collection} -> {out_file}")

    aliases = get_aliases(src_url)
    aliases_file = src_backup_dir / "aliases.json"
    aliases_file.write_text(json.dumps({"aliases": aliases}, indent=2), encoding="utf-8")

    # 3. Delete destination collections if enabled
    if delete_before_restore:
        if collection_mode == "single":
            delete_list = [collection_name] if collection_name in dst_collections else []
        else:
            delete_list = [
                c for c in dst_collections
                if c not in preserve_collections
            ]

        for collection in delete_list:
            if collection in preserve_collections:
                log.append(f"Preserved destination collection: {collection}")
                continue

            log.append(f"Deleting destination collection: {collection}")
            qdrant_delete_json(dst_url, f"/collections/{collection}?wait=true")

    # 4. Restore source snapshots into destination
    log.append("Restoring source snapshots into destination...")

    for collection in collections_to_restore:
        if collection in preserve_collections:
            log.append(f"Skipping preserved collection restore: {collection}")
            continue

        snapshot_path = Path(source_snapshots[collection])
        upload_result = upload_snapshot(dst_url, collection, snapshot_path)
        log.append(f"Restored collection: {collection} -> {upload_result}")

    # 5. Restore aliases
    alias_results = []

    if restore_aliases:
        log.append("Restoring source aliases...")

        restored_set = set(collections_to_restore)

        for alias in aliases:
            alias_name = alias.get("alias_name")
            alias_collection = alias.get("collection_name")

            if not alias_name or not alias_collection:
                continue

            if alias_collection in preserve_collections:
                log.append(f"Skipping alias for preserved collection: {alias_name} -> {alias_collection}")
                continue

            if alias_collection not in restored_set:
                continue

            try:
                res = create_alias(dst_url, alias_name, alias_collection)
                alias_results.append({
                    "alias_name": alias_name,
                    "collection_name": alias_collection,
                    "result": res,
                })
                log.append(f"Alias restored: {alias_name} -> {alias_collection}")
            except Exception as exc:
                alias_results.append({
                    "alias_name": alias_name,
                    "collection_name": alias_collection,
                    "error": str(exc),
                })
                log.append(f"Alias restore failed: {alias_name} -> {alias_collection}: {exc}")

    # 6. Verify counts
    for collection in collections_to_restore:
        verification.append({
            "collection": collection,
            "source_points": point_count(src_url, collection),
            "destination_points": point_count(dst_url, collection),
        })

    preserved_counts = []
    for collection in preserve_collections:
        if collection:
            preserved_counts.append({
                "collection": collection,
                "destination_points": point_count(dst_url, collection),
            })

    final_dst_collections = list_collections(dst_url)
    elapsed = round(time.time() - started, 2)

    return {
        "status": "ok",
        "source_url": src_url,
        "destination_url": dst_url,
        "collection_mode": collection_mode,
        "collection_name": collection_name,
        "preserve_collections": sorted(preserve_collections),
        "delete_before_restore": delete_before_restore,
        "backup_destination": backup_destination,
        "restore_aliases": restore_aliases,
        "work_dir": str(work_dir),
        "collections_restored": collections_to_restore,
        "verification": verification,
        "preserved_counts": preserved_counts,
        "alias_results": alias_results,
        "final_destination_collections": final_dst_collections,
        "elapsed_seconds": elapsed,
        "log": log,
    }


@login_required
@require_http_methods(["GET", "POST"])
def qdrant_copy_restore_page(request: HttpRequest):
    if not _can_run_qdrant_copy_restore(request.user):
        return HttpResponseForbidden(
            "You do not have permission to run Qdrant copy/restore."
        )

    context = {
        "src_url": DEFAULT_SRC,
        "dst_url": DEFAULT_DST,
        "collection_mode": "single",
        "collection_name": "buro_website",
        "preserve_collections": DEFAULT_PRESERVE,
        "delete_before_restore": True,
        "backup_destination": True,
        "restore_aliases": False,
        "dry_run": True,
        "roles": sorted(_user_role_names(request.user)),
        "result": None,
        "result_json": "",
        "error": None,
    }

    if request.method == "GET":
        return render(request, "admin/qdrant_copy_restore.html", context)

    try:
        src_url = request.POST.get("src_url", DEFAULT_SRC).strip()
        dst_url = request.POST.get("dst_url", DEFAULT_DST).strip()
        collection_mode = request.POST.get("collection_mode", "single").strip()
        collection_name = request.POST.get("collection_name", "").strip()
        preserve_raw = request.POST.get("preserve_collections", DEFAULT_PRESERVE).strip()

        delete_before_restore = request.POST.get("delete_before_restore") == "on"
        backup_destination = request.POST.get("backup_destination") == "on"
        restore_aliases = request.POST.get("restore_aliases") == "on"
        dry_run = request.POST.get("dry_run") == "on"

        result = run_qdrant_copy_restore(
            src_url=src_url,
            dst_url=dst_url,
            collection_mode=collection_mode,
            collection_name=collection_name,
            preserve_collections=parse_preserve_collections(preserve_raw),
            delete_before_restore=delete_before_restore,
            backup_destination=backup_destination,
            restore_aliases=restore_aliases,
            dry_run=dry_run,
        )

        context.update({
            "src_url": src_url,
            "dst_url": dst_url,
            "collection_mode": collection_mode,
            "collection_name": collection_name,
            "preserve_collections": preserve_raw,
            "delete_before_restore": delete_before_restore,
            "backup_destination": backup_destination,
            "restore_aliases": restore_aliases,
            "dry_run": dry_run,
            "result": result,
            "result_json": json.dumps(result, indent=2, ensure_ascii=False),
        })

        return render(request, "admin/qdrant_copy_restore.html", context)

    except Exception as exc:
        context.update({
            "src_url": request.POST.get("src_url", DEFAULT_SRC),
            "dst_url": request.POST.get("dst_url", DEFAULT_DST),
            "collection_mode": request.POST.get("collection_mode", "single"),
            "collection_name": request.POST.get("collection_name", "buro_website"),
            "preserve_collections": request.POST.get("preserve_collections", DEFAULT_PRESERVE),
            "delete_before_restore": request.POST.get("delete_before_restore") == "on",
            "backup_destination": request.POST.get("backup_destination") == "on",
            "restore_aliases": request.POST.get("restore_aliases") == "on",
            "dry_run": request.POST.get("dry_run") == "on",
            "error": str(exc),
        })

        return render(request, "admin/qdrant_copy_restore.html", context)
