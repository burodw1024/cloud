from __future__ import annotations

import re

from chat.dynamic_config import get_setting

_tts_client = None
_tts_region = None


def detect_language(text):
    bangla_chars = len(re.findall(r"[\u0980-\u09FF]", text or ""))
    latin_chars = len(re.findall(r"[A-Za-z]", text or ""))
    return "bn-BD" if bangla_chars >= max(3, latin_chars // 2) else "en-US"


def strip_markdown_for_speech(text):
    text = re.sub(r"```.*?```", " ", text or "", flags=re.DOTALL)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"[*_~]+", "", text)
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\d+\.\s+", "", text, flags=re.MULTILINE)
    return re.sub(r"\s+", " ", text).strip()


def truncate_utf8(text, max_bytes=3600):
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    clipped = encoded[:max_bytes]
    while clipped:
        try:
            return clipped.decode("utf-8").rstrip() + "…"
        except UnicodeDecodeError:
            clipped = clipped[:-1]
    return ""


def get_tts_client():
    global _tts_client, _tts_region

    try:
        from google.api_core.client_options import ClientOptions
        from google.cloud import texttospeech
    except Exception as exc:
        raise RuntimeError(
            "Google TTS package is not installed. Browser voice can still be used."
        ) from exc

    region = get_setting("GOOGLE_CLOUD_REGION", "global").strip() or "global"

    if _tts_client is None or _tts_region != region:
        endpoint = (
            "texttospeech.googleapis.com"
            if region == "global"
            else "%s-texttospeech.googleapis.com" % region
        )
        _tts_client = texttospeech.TextToSpeechClient(
            client_options=ClientOptions(api_endpoint=endpoint)
        )
        _tts_region = region

    return _tts_client


def create_speech(text, language="auto", voice=None):
    try:
        from google.cloud import texttospeech
    except Exception as exc:
        raise RuntimeError("google-cloud-texttospeech is not installed.") from exc

    clean_text = strip_markdown_for_speech(text)
    if not clean_text:
        raise ValueError("No speakable text supplied.")

    language = detect_language(clean_text) if language in ("", "auto", None) else language
    if language not in {"bn-BD", "en-US", "en-GB"}:
        language = detect_language(clean_text)

    clean_text = truncate_utf8(clean_text, 3600)
    voice_name = (voice or get_setting("GOOGLE_TTS_VOICE", "Kore")).strip()
    model_name = get_setting("GOOGLE_TTS_MODEL", "gemini-2.5-flash-tts").strip()

    if language == "bn-BD":
        prompt = (
            "Speak in natural Bangladeshi Bangla. Use a warm, professional, calm "
            "policy-assistant tone. Pronounce Bangla naturally, clearly and smoothly. "
            "Use a moderate pace and natural pauses. Read numbers, dates and policy "
            "terms carefully. Do not sound theatrical."
        )
    else:
        prompt = (
            "Speak clearly in a warm, professional policy-assistant tone. "
            "Use a moderate pace and natural pauses."
        )

    synthesis_input = texttospeech.SynthesisInput(text=clean_text, prompt=prompt)
    voice_params = texttospeech.VoiceSelectionParams(
        language_code=language,
        name=voice_name,
        model_name=model_name,
    )
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3
    )
    result = get_tts_client().synthesize_speech(
        input=synthesis_input,
        voice=voice_params,
        audio_config=audio_config,
    )
    return result.audio_content
