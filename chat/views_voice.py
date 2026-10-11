from __future__ import annotations

import os
import requests

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST


VOICE_STT_URL = (
    os.getenv(
        "VOICE_STT_URL",
        "http://192.168.1.136:30900/v1/audio/transcriptions",
    )
    or ""
).strip()


def normalize_language(value: str) -> str:
    value = (value or "auto").strip().lower()

    if value in ["bn", "bn-bd", "bangla", "bengali", "বাংলা"]:
        return "bn"

    if value in ["en", "en-us", "en-gb", "english"]:
        return "en"

    return "auto"


@csrf_exempt
@require_POST
def voice_to_text(request):
    """
    Browser microphone audio -> Django -> offline Whisper STT -> text.

    Frontend sends:
      audio    = browser audio blob
      language = auto | bn | en
      model    = whisper-1

    Whisper service:
      http://192.168.1.136:30900/v1/audio/transcriptions
    """

    audio = request.FILES.get("audio")
    language = normalize_language(request.POST.get("language") or "auto")
    model = (request.POST.get("model") or "whisper-1").strip()

    if not audio:
        return JsonResponse(
            {
                "ok": False,
                "error": "audio file is required",
            },
            status=400,
        )

    if not VOICE_STT_URL:
        return JsonResponse(
            {
                "ok": False,
                "error": "VOICE_STT_URL is not configured",
            },
            status=500,
        )

    try:
        files = {
            "file": (
                audio.name or "voice.webm",
                audio.read(),
                audio.content_type or "audio/webm",
            )
        }

        data = {
            "model": model,
            "response_format": "json",
            "temperature": "0",
        }

        # Important for Bangla quality:
        # Do not send bn-BD. Send only bn.
        if language == "bn":
            data["language"] = "bn"
            data["prompt"] = (
                "বাংলা কথোপকথন। বাংলা ইউনিকোডে পরিষ্কারভাবে লিখুন। "
                "যদি ইংরেজি শব্দ থাকে যেমন loan amount, branch code, customer id, member id, "
                "সেগুলো ইংরেজিতেই রাখুন। বাংলা সংখ্যা থাকলে প্রয়োজনে সংখ্যায় লিখুন।"
            )
        elif language == "en":
            data["language"] = "en"
            data["prompt"] = "English voice transcription. Keep financial terms clear."

        response = requests.post(
            VOICE_STT_URL,
            files=files,
            data=data,
            timeout=180,
        )

        if not response.ok:
            return JsonResponse(
                {
                    "ok": False,
                    "error": f"STT service error {response.status_code}",
                    "details": response.text[:2000],
                    "stt_url": VOICE_STT_URL,
                    "language": language,
                },
                status=502,
            )

        try:
            stt_data = response.json()
        except Exception:
            return JsonResponse(
                {
                    "ok": False,
                    "error": "STT service returned non-JSON response",
                    "details": response.text[:2000],
                    "stt_url": VOICE_STT_URL,
                    "language": language,
                },
                status=502,
            )

        text = (stt_data.get("text") or "").strip()

        return JsonResponse(
            {
                "ok": True,
                "text": text,
                "engine": "whisper-server",
                "offline": True,
                "language": language,
                "model": model,
                "raw": stt_data,
            },
            json_dumps_params={"ensure_ascii": False},
        )

    except requests.RequestException as exc:
        return JsonResponse(
            {
                "ok": False,
                "error": f"Could not connect to STT service: {exc}",
                "stt_url": VOICE_STT_URL,
            },
            status=502,
        )

    except Exception as exc:
        return JsonResponse(
            {
                "ok": False,
                "error": f"Voice proxy error: {exc}",
                "stt_url": VOICE_STT_URL,
            },
            status=500,
        )


browser_voice_transcribe = voice_to_text
voice_transcribe = voice_to_text
