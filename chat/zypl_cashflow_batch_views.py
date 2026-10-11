from __future__ import annotations

import datetime
import json
import os
from typing import Any, Dict, Optional

import requests

from django.core.files.base import ContentFile
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .models import UploadedFile

from .zypl_cashflow_scoring_agent import (
    ZYPL_ALLOWED_ROLES,
    ZYPL_BASE_URL,
    ZYPL_MODEL_ID,
    ZYPL_PARTNER_ID,
    ZYPL_PASSWORD,
    ZYPL_REQUIRE_LOGIN,
    ZYPL_USERNAME,
)


EXCEL_XLSX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
EXCEL_XLS_CONTENT_TYPE = "application/vnd.ms-excel"


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value


def _json_error(
    message: str,
    status: int = 400,
    extra: Optional[Dict[str, Any]] = None,
):
    data = {
        "ok": False,
        "error": message,
    }

    if extra:
        data.update(extra)

    return JsonResponse(
        data,
        status=status,
        json_dumps_params={"ensure_ascii": False},
    )


def _user_has_zypl_access(request) -> bool:
    """
    Uses your existing Zypl role/login config.

    Supports:
    - ZYPL_REQUIRE_LOGIN=False
    - Django superuser
    - Django groups from ZYPL_ALLOWED_ROLES
    - user.role
    - user.profile.role
    """

    if not ZYPL_REQUIRE_LOGIN:
        return True

    user = getattr(request, "user", None)

    if not user or not getattr(user, "is_authenticated", False):
        return False

    if getattr(user, "is_superuser", False):
        return True

    allowed_roles = set(ZYPL_ALLOWED_ROLES or [])

    try:
        group_names = set(user.groups.values_list("name", flat=True))
        if group_names.intersection(allowed_roles):
            return True
    except Exception:
        pass

    try:
        user_role = getattr(user, "role", None)
        if user_role in allowed_roles:
            return True
    except Exception:
        pass

    try:
        profile = getattr(user, "profile", None)
        profile_role = getattr(profile, "role", None)
        if profile_role in allowed_roles:
            return True
    except Exception:
        pass

    return False


def _extract_token_from_response(data: Dict[str, Any]) -> Optional[str]:
    token_keys = [
        "access_token",
        "token",
        "jwt",
        "bearer",
        "accessToken",
    ]

    for key in token_keys:
        value = data.get(key)
        if value:
            return str(value)

    nested = data.get("data")
    if isinstance(nested, dict):
        for key in token_keys:
            value = nested.get(key)
            if value:
                return str(value)

    return None


def _get_zypl_bearer_token() -> str:
    """
    Uses existing Zypl env/config.

    Priority:
    1. ZYPL_ACCESS_TOKEN
    2. ZYPL_TOKEN
    3. ZYPL_BEARER_TOKEN
    4. Login using ZYPL_USERNAME and ZYPL_PASSWORD

    Optional:
        ZYPL_LOGIN_URL
    """

    env_token = (
        _env("ZYPL_ACCESS_TOKEN")
        or _env("ZYPL_TOKEN")
        or _env("ZYPL_BEARER_TOKEN")
    )

    if env_token:
        return env_token.strip()

    if not ZYPL_USERNAME or not ZYPL_PASSWORD:
        raise RuntimeError(
            "Zypl token not found. Set ZYPL_ACCESS_TOKEN or configure ZYPL_USERNAME and ZYPL_PASSWORD."
        )

    base_url = str(ZYPL_BASE_URL).rstrip("/")

    login_urls = []

    explicit_login_url = _env("ZYPL_LOGIN_URL")
    if explicit_login_url:
        login_urls.append(explicit_login_url.rstrip("/"))

    login_urls.extend(
        [
            f"{base_url}/auth/login",
            f"{base_url}/login",
            f"{base_url}/token",
        ]
    )

    login_payloads = [
        {
            "login": ZYPL_USERNAME,
            "password": ZYPL_PASSWORD,
        },
        {
            "username": ZYPL_USERNAME,
            "password": ZYPL_PASSWORD,
        },
    ]

    last_error = None

    for login_url in login_urls:
        for payload in login_payloads:
            try:
                response = requests.post(
                    login_url,
                    json=payload,
                    timeout=60,
                )

                if not response.ok:
                    last_error = (
                        f"{login_url} returned HTTP {response.status_code}: "
                        f"{response.text[:500]}"
                    )
                    continue

                try:
                    data = response.json()
                except Exception:
                    last_error = f"{login_url} returned non-JSON response."
                    continue

                token = _extract_token_from_response(data)

                if token:
                    return token.strip()

                last_error = f"{login_url} login succeeded but token key was not found."

            except Exception as exc:
                last_error = str(exc)

    raise RuntimeError(f"Could not get Zypl bearer token. Last error: {last_error}")


def _allowed_batch_extensions():
    """
    Zypl sample uses .xlsx.

    Default:
        .xlsx

    To allow .xls also:
        export ZYPL_BATCH_ALLOWED_EXT=".xlsx,.xls"
    """

    raw = _env("ZYPL_BATCH_ALLOWED_EXT", ".xlsx") or ".xlsx"

    return {
        item.strip().lower()
        for item in raw.split(",")
        if item.strip()
    }


def _excel_content_type(ext: str) -> str:
    ext = (ext or "").lower()

    if ext == ".xls":
        return EXCEL_XLS_CONTENT_TYPE

    return EXCEL_XLSX_CONTENT_TYPE


@csrf_exempt
@require_http_methods(["GET", "POST"])
def zypl_cashflow_score_file_from_uploaded_file_api(request):
    """
    Zypl batch file scoring using your existing UploadedFile system.

    Step 1:
        POST /api/files/upload
        form-data:
            file=@sample.xlsx

        Response:
            file_id = UUID

    Step 2:
        POST /api/zypl/cashflow/score-file/from-uploaded-file
        JSON:
            {
                "file_id": "UUID_FROM_UPLOAD_API"
            }

    This endpoint:
    - loads UploadedFile by UUID file_id
    - sends saved Excel to Zypl score_file API
    - saves Zypl response Excel as a new UploadedFile
    - returns output file_id and media_path
    """

    if request.method == "GET":
        return JsonResponse(
            {
                "ok": True,
                "module": "zypl_batch_scoring_from_uploaded_file",
                "description": "Uses existing /api/files/upload file_id and sends saved Excel file to Zypl score_file API.",
                "step_1_upload": {
                    "endpoint": "/api/files/upload",
                    "method": "POST",
                    "form_data": {
                        "file": "sample.xlsx"
                    },
                    "returns": {
                        "file_id": "UUID"
                    },
                },
                "step_2_process": {
                    "endpoint": "/api/zypl/cashflow/score-file/from-uploaded-file",
                    "method": "POST",
                    "json_payload": {
                        "file_id": "UUID_RETURNED_FROM_UPLOAD_API"
                    },
                },
                "zypl_remote_endpoint": f"{ZYPL_BASE_URL}/partner/{ZYPL_PARTNER_ID}/model/{ZYPL_MODEL_ID}/score_file",
                "allowed_extensions": sorted(list(_allowed_batch_extensions())),
            },
            json_dumps_params={"ensure_ascii": False},
        )

    if not _user_has_zypl_access(request):
        return _json_error(
            "Cashflow/Zypl module access required.",
            status=403,
            extra={
                "allowed_roles_or_groups": ZYPL_ALLOWED_ROLES,
            },
        )

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = {}

    file_id = (
        payload.get("file_id")
        or payload.get("uploaded_file_id")
        or payload.get("uuid")
    )

    if not file_id:
        return _json_error(
            "file_id is required. First upload the Excel file using /api/files/upload.",
            status=400,
        )

    try:
        input_file_obj = UploadedFile.objects.get(file_id=file_id)
    except UploadedFile.DoesNotExist:
        return _json_error(
            "Uploaded file not found for this file_id.",
            status=404,
            extra={
                "file_id": str(file_id),
            },
        )

    filename = (
        input_file_obj.filename
        or os.path.basename(input_file_obj.file.name or "")
        or "sample.xlsx"
    )

    ext = os.path.splitext(filename)[1].lower()
    allowed_ext = _allowed_batch_extensions()

    if ext not in allowed_ext:
        return _json_error(
            "This file type is not allowed for Zypl batch scoring.",
            status=400,
            extra={
                "filename": filename,
                "extension": ext,
                "allowed_extensions": sorted(list(allowed_ext)),
                "note": "Default is .xlsx. To allow .xls also, set ZYPL_BATCH_ALLOWED_EXT='.xlsx,.xls'.",
            },
        )

    if not input_file_obj.file:
        return _json_error(
            "Uploaded file record exists but physical file is missing.",
            status=404,
            extra={
                "file_id": str(input_file_obj.file_id),
            },
        )

    try:
        local_file_path = input_file_obj.file.path
    except Exception:
        local_file_path = ""

    if not local_file_path or not os.path.exists(local_file_path):
        return _json_error(
            "Uploaded physical file not found on server.",
            status=404,
            extra={
                "file_id": str(input_file_obj.file_id),
                "media_path": input_file_obj.file.url if input_file_obj.file else "",
            },
        )

    if not ZYPL_BASE_URL or not ZYPL_PARTNER_ID or not ZYPL_MODEL_ID:
        return _json_error(
            "ZYPL_BASE_URL, ZYPL_PARTNER_ID, and ZYPL_MODEL_ID are required.",
            status=500,
        )

    zypl_url = (
        f"{str(ZYPL_BASE_URL).rstrip('/')}"
        f"/partner/{ZYPL_PARTNER_ID}"
        f"/model/{ZYPL_MODEL_ID}"
        f"/score_file"
    )

    try:
        token = _get_zypl_bearer_token()
    except Exception as exc:
        return _json_error(
            f"Could not prepare Zypl authorization token: {str(exc)}",
            status=500,
        )

    headers = {
        "Authorization": f"Bearer {token}",
    }

    timeout_seconds = int(
        _env(
            "ZYPL_BATCH_TIMEOUT_SECONDS",
            _env("ZYPL_TIMEOUT_SECONDS", "600"),
        )
    )

    try:
        with open(local_file_path, "rb") as excel_file:
            files = {
                "file": (
                    filename,
                    excel_file,
                    _excel_content_type(ext),
                )
            }

            zypl_response = requests.post(
                zypl_url,
                headers=headers,
                files=files,
                timeout=timeout_seconds,
            )

    except requests.exceptions.Timeout:
        return _json_error(
            "Zypl batch scoring request timed out.",
            status=504,
            extra={
                "input_file_id": str(input_file_obj.file_id),
                "zypl_endpoint": zypl_url,
                "timeout_seconds": timeout_seconds,
            },
        )

    except requests.exceptions.RequestException as exc:
        return _json_error(
            f"Zypl batch scoring request failed: {str(exc)}",
            status=502,
            extra={
                "input_file_id": str(input_file_obj.file_id),
                "zypl_endpoint": zypl_url,
            },
        )

    if not zypl_response.ok:
        content_type = zypl_response.headers.get("Content-Type", "")

        if "application/json" in content_type:
            try:
                zypl_error = zypl_response.json()
            except Exception:
                zypl_error = zypl_response.text[:2000]
        else:
            zypl_error = zypl_response.text[:2000]

        return _json_error(
            "Zypl batch scoring API returned an error.",
            status=zypl_response.status_code,
            extra={
                "input_file_id": str(input_file_obj.file_id),
                "zypl_status": zypl_response.status_code,
                "zypl_error": zypl_error,
            },
        )

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    output_filename = f"zypl_batch_score_result_{timestamp}.xlsx"

    user = getattr(request, "user", None)
    if not user or not getattr(user, "is_authenticated", False):
        user = None

    output_file_obj = UploadedFile(
        user=user,
        filename=output_filename,
        content_type=EXCEL_XLSX_CONTENT_TYPE,
        extracted_text="",
    )

    output_file_obj.file.save(
        output_filename,
        ContentFile(zypl_response.content),
        save=True,
    )

    return JsonResponse(
        {
            "ok": True,
            "message": "Zypl batch file scoring completed successfully.",
            "input": {
                "file_id": str(input_file_obj.file_id),
                "db_id": input_file_obj.id,
                "filename": input_file_obj.filename,
                "media_path": input_file_obj.file.url if input_file_obj.file else "",
            },
            "output": {
                "file_id": str(output_file_obj.file_id),
                "db_id": output_file_obj.id,
                "filename": output_file_obj.filename,
                "media_path": output_file_obj.file.url if output_file_obj.file else "",
            },
            "zypl": {
                "status": zypl_response.status_code,
                "endpoint": zypl_url,
                "partner_id": ZYPL_PARTNER_ID,
                "model_id": ZYPL_MODEL_ID,
            },
        },
        json_dumps_params={"ensure_ascii": False},
    )
