# chat/views_policy.py
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

import requests
from django.http import JsonResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

JsonT = Union[Dict[str, Any], List[Any]]


# =========================================================
# Config
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
    # Qdrant
    QDRANT_URL: str = _env("QDRANT_URL", "http://192.168.1.176:31633").rstrip("/")
    POLICY_COLLECTION: str = _env("POLICY_COLLECTION", "buro_policy")
    QDRANT_TIMEOUT: float = _env_float("QDRANT_TIMEOUT", "25")

    # Ollama embeddings
    OLLAMA_URL: str = _env("OLLAMA_URL", "http://192.168.1.144:31134").rstrip("/")
    EMBED_MODEL: str = _env("EMBED_MODEL", "nomic-embed-text")
    EMBED_TIMEOUT: float = _env_float("EMBED_TIMEOUT", "120")

    # Ollama chat
    CHAT_MODEL: str = _env("CHAT_MODEL", "aya-23-8b:q5")
    CHAT_TIMEOUT: float = _env_float("CHAT_TIMEOUT", "240")
    CHAT_TEMPERATURE: float = _env_float("CHAT_TEMPERATURE", "0.2")
    CHAT_NUM_PREDICT: int = _env_int("CHAT_NUM_PREDICT", "220")
    CHAT_NUM_CTX: int = _env_int("CHAT_NUM_CTX", "2048")
    CHAT_KEEP_ALIVE: str = _env("CHAT_KEEP_ALIVE", "10m")

    # Retrieval
    TOP_K: int = _env_int("POLICY_TOP_K", "6")
    SCORE_THRESHOLD: float = _env_float("POLICY_SCORE_THRESHOLD", "0.15")

    # Context
    MAX_CTX_CHARS: int = _env_int("POLICY_MAX_CTX_CHARS", "7000")
    MAX_EXCERPT_LINES: int = _env_int("POLICY_MAX_EXCERPT_LINES", "30")
    MAX_EXCERPTS: int = _env_int("POLICY_MAX_EXCERPTS", "6")

    # Default behavior
    USE_CHAT_ANSWER: int = _env_int("POLICY_USE_CHAT_ANSWER", "1")


CFG = Cfg()


# =========================================================
# Basic helpers
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


def _norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", _clean(s)).strip()


def _normalize_for_match(text: str) -> str:
    s = _clean(text).lower()

    s = s.replace("–", "-").replace("—", "-").replace("_", " ")
    s = s.replace("ঃ", " ").replace("(", " ").replace(")", " ")
    s = s.replace("/", " ").replace("\\", " ").replace(",", " ")
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')

    s = s.replace("employees", "employee")
    s = s.replace("staffs", "staff")
    s = s.replace("types", "type")
    s = s.replace("rules", "rule")
    s = s.replace("benefits", "benefit")

    s = s.replace("চাকুরী", "চাকরি")
    s = s.replace("ছুটী", "ছুটি")
    s = s.replace("বদলী", "বদলি")

    s = re.sub(r"[^0-9a-z\u0980-\u09ff\-\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _is_bn(text: str) -> bool:
    return bool(re.search(r"[\u0980-\u09FF]", text or ""))


def _http_json(method: str, url: str, *, timeout: float, json_body: Optional[dict] = None) -> JsonT:
    r = requests.request(
        method,
        url,
        json=json_body,
        timeout=timeout,
        headers={"Accept": "application/json"},
    )
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code} from {url}: {r.text[:1200]}")
    try:
        return r.json()
    except Exception as e:
        raise RuntimeError(f"Invalid JSON from {url}: {e}. Body={r.text[:1200]}")


def _ndjson(obj: Dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False) + "\n"


# =========================================================
# Ollama
# =========================================================
def _ollama_embed(text: str) -> List[float]:
    data = _http_json(
        "POST",
        f"{CFG.OLLAMA_URL}/api/embeddings",
        timeout=CFG.EMBED_TIMEOUT,
        json_body={"model": CFG.EMBED_MODEL, "prompt": text},
    )
    if not isinstance(data, dict):
        raise RuntimeError("Embedding response is not a JSON object.")

    emb = data.get("embedding")
    if not isinstance(emb, list) or not emb:
        raise RuntimeError(f"Embedding missing/empty from Ollama response: {data}")

    return [float(x) for x in emb]


SYSTEM_POLICY = """You are an HR/policy assistant.
You MUST answer ONLY from the provided excerpts.

Format EXACTLY:
Summary: <2-4 lines>
Rules:
- <5-8 complete bullets>
Sources: [1], [2]...

Rules:
- No repetition.
- Do not mix unrelated topics.
- Do not invent.
- If policy is not clear, say: "Policy does not state this clearly."
- If question is in Bangla, answer in Bangla.
"""


def _ollama_chat(messages: List[Dict[str, str]]) -> str:
    req = {
        "model": CFG.CHAT_MODEL,
        "stream": False,
        "messages": messages,
        "keep_alive": CFG.CHAT_KEEP_ALIVE,
        "options": {
            "temperature": float(CFG.CHAT_TEMPERATURE),
            "num_predict": int(CFG.CHAT_NUM_PREDICT),
            "num_ctx": int(CFG.CHAT_NUM_CTX),
            "top_p": 0.9,
        },
    }
    try:
        data = _http_json(
            "POST",
            f"{CFG.OLLAMA_URL}/api/chat",
            timeout=CFG.CHAT_TIMEOUT,
            json_body=req,
        )
    except Exception:
        return ""

    if not isinstance(data, dict):
        return ""

    return _clean(((data.get("message") or {}).get("content")))


def _ollama_chat_stream(messages: List[Dict[str, str]]) -> Iterable[str]:
    req = {
        "model": CFG.CHAT_MODEL,
        "stream": True,
        "messages": messages,
        "keep_alive": CFG.CHAT_KEEP_ALIVE,
        "options": {
            "temperature": float(CFG.CHAT_TEMPERATURE),
            "num_predict": int(CFG.CHAT_NUM_PREDICT),
            "num_ctx": int(CFG.CHAT_NUM_CTX),
            "top_p": 0.9,
        },
    }

    try:
        with requests.post(
            f"{CFG.OLLAMA_URL}/api/chat",
            json=req,
            timeout=CFG.CHAT_TIMEOUT,
            stream=True,
            headers={"Accept": "application/json"},
        ) as r:
            if not r.ok:
                yield _ndjson(
                    {
                        "type": "error",
                        "message": f"Ollama HTTP {r.status_code}: {r.text[:1000]}",
                    }
                )
                return

            for raw_line in r.iter_lines(decode_unicode=True):
                if not raw_line:
                    continue

                try:
                    obj = json.loads(raw_line)
                except Exception:
                    continue

                msg = obj.get("message") or {}
                token = msg.get("content") or ""
                done = bool(obj.get("done"))

                if token:
                    yield _ndjson({"type": "token", "content": token})

                if done:
                    yield _ndjson({"type": "done"})
                    break

    except Exception as e:
        yield _ndjson({"type": "error", "message": str(e)})


# =========================================================
# Manual intent aliases for your clause-based topics
# =========================================================
INTENT_ALIASES: Dict[str, List[str]] = {
    "staff_classification": [
        "types of staff",
        "type of staff",
        "staff type",
        "staff classification",
        "classification of staff",
        "employee type",
        "employee classification",
        "কর্মীদের শ্রেণী বিভাগ",
        "কর্মীদের ধরন",
        "কর্মীর ধরন",
        "কর্মীদের প্রকারভেদ",
        "স্টাফের ধরন",
        "স্টাফ টাইপ",
    ],
    "departments": [
        "departments of the organization",
        "organization departments",
        "department list",
        "সংস্থার বিভাগসমূহ",
        "বিভাগসমূহ",
        "বিভাগের তালিকা",
    ],
    "grade_structure": [
        "grade structure",
        "grades",
        "posts and grades",
        "গ্রেড কাঠামো",
        "গ্রেড",
        "পদ ও গ্রেড",
    ],
    "recruitment_advertisement": [
        "recruitment advertisement",
        "job circular",
        "নিয়োগ বিজ্ঞপ্তি",
        "নিয়োগ সার্কুলার",
    ],
    "age_limit_recruitment": [
        "age limit for recruitment",
        "recruitment age limit",
        "age limit",
        "নিয়োগের বয়সসীমা",
        "বয়সসীমা",
    ],
    "recruitment_board": [
        "recruitment board",
        "selection board",
        "নিয়োগ বোর্ড",
        "নিয়োগ কমিটি",
    ],
    "joining_rules": [
        "joining rules",
        "rules of joining",
        "joining policy",
        "যোগদান বিধি",
        "যোগদানের নিয়ম",
        "চাকরিতে যোগদান",
    ],
    "basic_transfer_policy": [
        "transfer policy",
        "basic transfer policy",
        "employee transfer policy",
        "বদলি নীতি",
        "বদলির নীতি",
        "বদলির মৌলিক নীতি",
        "বদলী নীতি",
    ],
    "transfer_notice_period": [
        "transfer notice period",
        "notice before transfer",
        "বদলির নোটিশ",
        "বদলীর নোটিশ",
        "বদলির নোটিশ সময়",
    ],
    "transfer_tenure_rules": [
        "transfer tenure",
        "how long before transfer",
        "বদলির সময়কাল",
        "কত বছর পরে বদলি",
    ],
    "attendance_recording": [
        "attendance recording",
        "attendance record",
        "হাজিরা রেকর্ড",
        "উপস্থিতির রেকর্ড",
    ],
    "late_attendance_absence": [
        "late attendance",
        "absence rule",
        "late and absence",
        "বিলম্বে উপস্থিতি",
        "অনুপস্থিতি",
        "দেরিতে আসা",
    ],
    "probation_confirmation": [
        "probation",
        "confirmation",
        "probation and confirmation",
        "শিক্ষানবিশকাল",
        "স্থায়ীকরণ",
        "শিক্ষানবিশকাল ও স্থায়ীকরণ",
    ],
    "seniority_register_basis": [
        "seniority",
        "seniority basis",
        "জ্যেষ্ঠতা",
        "জ্যেষ্ঠতার ভিত্তি",
    ],
    "promotion_basic": [
        "promotion policy",
        "basic promotion principles",
        "পদোন্নতির নীতি",
        "পদোন্নতির মৌলিক নীতি",
        "promotion principles",
    ],
    "promotion_eligibility": [
        "promotion eligibility",
        "promotion service period",
        "পদোন্নতির যোগ্যতা",
        "পদোন্নতির সময়সীমা",
    ],
    "annual_appraisal": [
        "performance appraisal",
        "annual appraisal",
        "বার্ষিক কর্মমূল্যায়ন",
        "কর্মমূল্যায়ন",
    ],
    "sexual_harassment_prevention": [
        "sexual harassment prevention",
        "যৌন হয়রানি প্রতিরোধ",
    ],
    "sexual_harassment_penalty": [
        "sexual harassment penalty",
        "penalty scale for sexual harassment",
        "যৌন হয়রানির শাস্তির ধাপ",
        "যৌন হয়রানির শাস্তি",
    ],
    "show_cause_notice": [
        "show cause notice",
        "কারণ দর্শানো নোটিশ",
        "শোকজ",
    ],
    "warning_letter": [
        "warning letter",
        "সতর্কীকরণ পত্র",
    ],
    "distance_allowance": [
        "distance allowance",
        "allowance for distance",
        "দূরত্ব ভাতা",
    ],
    "daily_travel_allowance": [
        "daily travel allowance",
        "travel allowance",
        "দৈনন্দিন যাতায়াত ভাতা",
        "যাতায়াত ভাতা",
    ],
    "food_lodging_daily": [
        "food lodging and daily allowance",
        "daily allowance",
        "খাদ্য অবস্থান ও দৈনিক ভাতা",
        "দৈনিক ভাতা",
        "অবস্থান ভাতা",
    ],
    "overtime_allowance": [
        "overtime allowance",
        "অধিকাল ভাতা",
        "ওভারটাইম",
    ],
    "transfer_allowance": [
        "transfer allowance",
        "বদলি ভাতা",
        "স্থানান্তর ভাতা",
        "বদলি/স্থানান্তর ভাতা",
    ],
    "salary_allowance_structure": [
        "salary and allowance structure",
        "salary structure",
        "বেতন ও ভাতা পরিকল্পনা",
        "বেতন কাঠামো",
    ],
    "gratuity_policy": [
        "gratuity",
        "gratuity policy",
        "গ্র্যাচুইটি",
        "গ্র্যাচুইটি নীতি",
    ],
    "provident_fund": [
        "provident fund",
        "pf",
        "প্রভিডেন্ট ফান্ড",
    ],
    "income_tax": [
        "income tax liability",
        "income tax",
        "আয়কর",
        "আয়কর দায়",
    ],
    "resignation_rules": [
        "resignation rules",
        "rules of resignation",
        "পদত্যাগের নিয়ম",
        "পদত্যাগ",
    ],
    "retirement_normal": [
        "normal retirement",
        "retirement",
        "অবসর",
        "স্বাভাবিক অবসর",
    ],
    "termination_general": [
        "termination",
        "grounds for termination",
        "অব্যাহতির কারণ",
        "অব্যাহতির সাধারণ কারণ",
    ],
    "leave_general": [
        "general leave rules",
        "leave policy",
        "ছুটির সাধারণ নীতি",
        "ছুটির নীতি",
        "ছুটি",
    ],
    "earned_leave": [
        "earned leave",
        "অর্জিত ছুটি",
    ],
    "casual_leave": [
        "casual leave",
        "নৈমিত্তিক ছুটি",
    ],
    "maternity_leave": [
        "maternity leave",
        "মাতৃত্বজনিত ছুটি",
    ],
    "paternity_leave": [
        "paternity leave",
        "পিতৃত্বজনিত ছুটি",
    ],
    "festival_leave": [
        "festival leave",
        "উৎসবকালীন ছুটি",
    ],
    "sick_leave": [
        "sick leave",
        "অসুস্থতাজনিত ছুটি",
    ],
    "higher_education_leave": [
        "higher education leave",
        "study leave",
        "উচ্চ শিক্ষা ছুটি",
    ],
    "leave_authority": [
        "leave sanctioning authority",
        "who can approve leave",
        "ছুটি অনুমোদনকারী কর্তৃপক্ষ",
    ],
}


MANUAL_TOPIC_RULES: List[Dict[str, str]] = [
    {"key": "staff_classification", "topic_id_prefix": "policy_1_1_", "topic_bn": "কর্মীদের শ্রেণী বিভাগ", "topic_en": "Staff Classification"},
    {"key": "departments", "topic_id": "policy_1_2_1_1_2_12", "topic_bn": "সংস্থার বিভাগসমূহ", "topic_en": "Departments of the Organization"},
    {"key": "grade_structure", "topic_id": "policy_1_3_1", "topic_bn": "গ্রেড কাঠামো", "topic_en": "Grade Structure"},
    {"key": "recruitment_advertisement", "topic_id": "policy_1_5_1", "topic_bn": "নিয়োগ বিজ্ঞপ্তি", "topic_en": "Recruitment Advertisement"},
    {"key": "age_limit_recruitment", "topic_id": "policy_1_5_5", "topic_bn": "নিয়োগের বয়সসীমা", "topic_en": "Age Limit for Recruitment"},
    {"key": "recruitment_board", "topic_id": "policy_1_6_1_1_6_4", "topic_bn": "নিয়োগ বোর্ড", "topic_en": "Recruitment Board"},
    {"key": "joining_rules", "topic_id": "policy_1_7_3_1_1_7_3_5", "topic_bn": "যোগদান বিধি", "topic_en": "Joining Rules"},
    {"key": "basic_transfer_policy", "topic_id": "policy_1_8_1_1_8_4", "topic_bn": "বদলীর মৌলিক নীতি", "topic_en": "Basic Transfer Policy"},
    {"key": "transfer_notice_period", "topic_id": "policy_1_8_9", "topic_bn": "বদলীর নোটিশ সময়", "topic_en": "Transfer Notice Period"},
    {"key": "transfer_tenure_rules", "topic_id": "policy_1_8_6_1_8_8", "topic_bn": "বদলীর সময়কাল", "topic_en": "Transfer Tenure Rules"},
    {"key": "attendance_recording", "topic_id": "policy_1_11_1", "topic_bn": "হাজিরা রেকর্ড", "topic_en": "Attendance Recording"},
    {"key": "late_attendance_absence", "topic_id": "policy_1_11_2_1_11_4", "topic_bn": "বিলম্বে উপস্থিতি ও অনুপস্থিতি", "topic_en": "Late Attendance and Absence"},
    {"key": "probation_confirmation", "topic_id": "policy_1_12_1_1_12_4", "topic_bn": "শিক্ষানবিশকাল ও স্থায়ীকরণ", "topic_en": "Probation and Confirmation"},
    {"key": "seniority_register_basis", "topic_id": "policy_2_1_1_2_1_3", "topic_bn": "জ্যেষ্ঠতা তালিকা ও ভিত্তি", "topic_en": "Seniority Register and Basis"},
    {"key": "promotion_basic", "topic_id": "policy_2_2_1_2_2_3", "topic_bn": "পদোন্নতির মৌলিক নীতি", "topic_en": "Basic Promotion Principles"},
    {"key": "promotion_eligibility", "topic_id": "policy_2_2_4", "topic_bn": "পদোন্নতির যোগ্যতা ও সময়সীমা", "topic_en": "Promotion Eligibility and Service Period"},
    {"key": "annual_appraisal", "topic_id": "policy_2_4_1_2_4_4", "topic_bn": "বার্ষিক কর্মমূল্যায়ন", "topic_en": "Annual Performance Appraisal"},
    {"key": "sexual_harassment_prevention", "topic_id": "policy_3_1_2(11)", "topic_bn": "যৌন হয়রানি প্রতিরোধ", "topic_en": "Prevention of Sexual Harassment"},
    {"key": "sexual_harassment_penalty", "topic_id": "policy_3_6_3_5", "topic_bn": "যৌন হয়রানির শাস্তির ধাপ", "topic_en": "Penalty Scale for Sexual Harassment"},
    {"key": "show_cause_notice", "topic_id": "policy_3_4_1", "topic_bn": "কারণ দর্শানো নোটিশ", "topic_en": "Show Cause Notice"},
    {"key": "warning_letter", "topic_id": "policy_3_5_1", "topic_bn": "সতর্কীকরণ পত্র", "topic_en": "Warning Letter"},
    {"key": "distance_allowance", "topic_id": "policy_6_8_1_6_8_3", "topic_bn": "দূরত্ব ভাতা", "topic_en": "Distance Allowance"},
    {"key": "daily_travel_allowance", "topic_id": "policy_6_5_1_6_5_9", "topic_bn": "দৈনন্দিন যাতায়াত ভাতা", "topic_en": "Daily Travel Allowance"},
    {"key": "food_lodging_daily", "topic_id": "policy_6_6_1_6_6_10", "topic_bn": "খাদ্য, অবস্থান ও দৈনিক ভাতা", "topic_en": "Food, Lodging and Daily Allowance"},
    {"key": "overtime_allowance", "topic_id": "policy_6_7_1_6_7_16", "topic_bn": "অধিকাল ভাতা", "topic_en": "Overtime Allowance"},
    {"key": "transfer_allowance", "topic_id": "policy_6_9_1_6_9_4", "topic_bn": "বদলি/স্থানান্তর ভাতা", "topic_en": "Transfer Allowance"},
    {"key": "salary_allowance_structure", "topic_id": "policy_6_1_1_6_1_7", "topic_bn": "বেতন ও ভাতা পরিকল্পনা", "topic_en": "Salary and Allowance Structure"},
    {"key": "gratuity_policy", "topic_id": "policy_6_2_1_6_2_9", "topic_bn": "গ্র্যাচুইটি নীতি", "topic_en": "Gratuity Policy"},
    {"key": "provident_fund", "topic_id": "policy_6_3_1_6_3_10", "topic_bn": "প্রভিডেন্ট ফান্ড", "topic_en": "Provident Fund"},
    {"key": "income_tax", "topic_id": "policy_6_4_1_6_4_5", "topic_bn": "আয়কর দায়", "topic_en": "Income Tax Liability"},
    {"key": "resignation_rules", "topic_id": "policy_4_2_1_4_2_4", "topic_bn": "পদত্যাগের নিয়ম", "topic_en": "Rules of Resignation"},
    {"key": "retirement_normal", "topic_id": "policy_4_3_1_4_3_4", "topic_bn": "স্বাভাবিক অবসর ও অবসরোত্তর চুক্তি", "topic_en": "Normal Retirement and Post-retirement Contract"},
    {"key": "termination_general", "topic_id": "policy_4_4_1_4_4_3", "topic_bn": "অব্যাহতির সাধারণ কারণ", "topic_en": "General Grounds for Termination"},
    {"key": "leave_general", "topic_id": "policy_5_1_1_5_1_4", "topic_bn": "ছুটির সাধারণ নীতি", "topic_en": "General Leave Rules"},
    {"key": "earned_leave", "topic_id": "policy_5_2_3", "topic_bn": "অর্জিত ছুটি", "topic_en": "Earned Leave"},
    {"key": "casual_leave", "topic_id": "policy_5_2_4", "topic_bn": "নৈমিত্তিক ছুটি", "topic_en": "Casual Leave"},
    {"key": "maternity_leave", "topic_id": "policy_5_2_5", "topic_bn": "মাতৃত্বজনিত ছুটি", "topic_en": "Maternity Leave"},
    {"key": "paternity_leave", "topic_id": "policy_5_2_6", "topic_bn": "পিতৃত্বজনিত ছুটি", "topic_en": "Paternity Leave"},
    {"key": "festival_leave", "topic_id": "policy_5_2_7", "topic_bn": "উৎসবকালীন ছুটি", "topic_en": "Festival Leave"},
    {"key": "sick_leave", "topic_id": "policy_5_2_8", "topic_bn": "অসুস্থতাজনিত ছুটি", "topic_en": "Sick Leave"},
    {"key": "higher_education_leave", "topic_id": "policy_5_2_9", "topic_bn": "উচ্চ শিক্ষা ছুটি", "topic_en": "Higher Education Leave"},
    {"key": "leave_authority", "topic_id": "policy_5_3_1_5_3_22", "topic_bn": "ছুটি অনুমোদনকারী কর্তৃপক্ষ", "topic_en": "Leave Sanctioning Authority"},
]


# =========================================================
# Topic catalog from Qdrant
# =========================================================
def _detect_vector_name(collection: str) -> Optional[str]:
    info = _http_json("GET", f"{CFG.QDRANT_URL}/collections/{collection}", timeout=CFG.QDRANT_TIMEOUT)
    if not isinstance(info, dict):
        return None

    result = info.get("result") or {}
    params = ((result.get("config") or {}).get("params") or {})
    vectors = params.get("vectors")

    if isinstance(vectors, dict) and "size" in vectors and "distance" in vectors:
        return None

    if isinstance(vectors, dict):
        if "default" in vectors:
            return "default"
        for k, v in vectors.items():
            if isinstance(v, dict) and "size" in v:
                return str(k)

    return None


def _qdrant_scroll(collection: str, *, limit: int = 1000, qfilter: Optional[dict] = None) -> List[dict]:
    payload = {
        "limit": limit,
        "with_payload": True,
        "with_vector": False,
    }
    if qfilter:
        payload["filter"] = qfilter

    data = _http_json(
        "POST",
        f"{CFG.QDRANT_URL}/collections/{collection}/points/scroll",
        timeout=CFG.QDRANT_TIMEOUT,
        json_body=payload,
    )
    if not isinstance(data, dict):
        return []

    result = data.get("result") or {}
    points = result.get("points") or []
    return points if isinstance(points, list) else []


_TOPIC_CACHE: Dict[str, List[Dict[str, str]]] = {}


def _get_topic_catalog(collection: str) -> List[Dict[str, str]]:
    if collection in _TOPIC_CACHE:
        return _TOPIC_CACHE[collection]

    points = _qdrant_scroll(collection, limit=1000)
    catalog: List[Dict[str, str]] = []
    seen: set[str] = set()

    for p in points:
        payload = p.get("payload") or {}
        topic_id = _clean(payload.get("topic_id") or payload.get("id"))
        topic_bn = _clean(payload.get("topic_bn"))
        topic_en = _clean(payload.get("topic_en"))
        clause_range = _clean(payload.get("clause_range"))
        source_file = _clean(payload.get("source_file"))

        key = "|".join([topic_id, topic_bn, topic_en, clause_range, source_file])
        if not key.strip() or key in seen:
            continue
        seen.add(key)

        catalog.append(
            {
                "topic_id": topic_id,
                "topic_bn": topic_bn,
                "topic_en": topic_en,
                "clause_range": clause_range,
                "source_file": source_file,
            }
        )

    _TOPIC_CACHE[collection] = catalog
    return catalog


# =========================================================
# Topic detection
# =========================================================
def _score_topic(question: str, topic: Dict[str, str]) -> int:
    q = _normalize_for_match(question)
    q_tokens = set(q.split())

    topic_id = _normalize_for_match(topic.get("topic_id", ""))
    topic_bn = _normalize_for_match(topic.get("topic_bn", ""))
    topic_en = _normalize_for_match(topic.get("topic_en", ""))
    clause = _normalize_for_match(topic.get("clause_range", ""))

    hay = " ".join([topic_id, topic_bn, topic_en, clause]).strip()
    hay_tokens = set(hay.split())

    score = 0

    if topic_bn and topic_bn in q:
        score += 120
    if topic_en and topic_en in q:
        score += 120
    if topic_id and topic_id in q:
        score += 140
    if clause and clause in q:
        score += 130

    overlap = q_tokens & hay_tokens
    score += len(overlap) * 8

    if q and q in hay:
        score += 60
    if hay and hay in q:
        score += 45

    return score


def _resolve_manual_topic(question: str) -> Optional[Dict[str, str]]:
    q = _normalize_for_match(question)

    for rule in MANUAL_TOPIC_RULES:
        aliases = INTENT_ALIASES.get(rule["key"], [])
        for alias in aliases:
            if _normalize_for_match(alias) in q:
                return rule
    return None


def _detect_best_topic(question: str, collection: str) -> Optional[Dict[str, str]]:
    manual = _resolve_manual_topic(question)
    topics = _get_topic_catalog(collection)

    if manual:
        topic_id = _clean(manual.get("topic_id"))
        topic_bn = _clean(manual.get("topic_bn"))
        topic_en = _clean(manual.get("topic_en"))
        topic_id_prefix = _clean(manual.get("topic_id_prefix"))

        for t in topics:
            if topic_id and _clean(t.get("topic_id")) == topic_id:
                return t
            if topic_bn and _clean(t.get("topic_bn")) == topic_bn:
                return t
            if topic_en and _clean(t.get("topic_en")) == topic_en:
                return t
            if topic_id_prefix and _clean(t.get("topic_id")).startswith(topic_id_prefix):
                return t

        return {
            "topic_id": topic_id or topic_id_prefix,
            "topic_bn": topic_bn,
            "topic_en": topic_en,
            "clause_range": "",
            "source_file": "",
        }

    if not topics:
        return None

    scored: List[Tuple[int, Dict[str, str]]] = []
    for t in topics:
        s = _score_topic(question, t)
        if s > 0:
            scored.append((s, t))

    if not scored:
        return None

    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best = scored[0]

    if best_score < 18:
        return None

    return best


def _build_exact_topic_filter(topic: Optional[Dict[str, str]]) -> Optional[dict]:
    if not topic:
        return None

    topic_id = _clean(topic.get("topic_id"))
    topic_bn = _clean(topic.get("topic_bn"))
    topic_en = _clean(topic.get("topic_en"))
    clause_range = _clean(topic.get("clause_range"))

    must = []
    if topic_id:
        if topic_id.endswith("_") or (topic_id.startswith("policy_") and not clause_range and not topic_bn):
            return {"must": [{"key": "topic_id", "match": {"text": topic_id}}]}
        must.append({"key": "topic_id", "match": {"value": topic_id}})
        return {"must": must}

    should = []
    if topic_bn:
        should.append({"key": "topic_bn", "match": {"value": topic_bn}})
    if topic_en:
        should.append({"key": "topic_en", "match": {"value": topic_en}})
    if clause_range:
        should.append({"key": "clause_range", "match": {"value": clause_range}})

    return {"should": should} if should else None


def _rewrite_query(question: str, detected_topic: Optional[Dict[str, str]]) -> str:
    q = _clean(question)
    if detected_topic:
        topic_bn = _clean(detected_topic.get("topic_bn"))
        topic_en = _clean(detected_topic.get("topic_en"))
        clause_range = _clean(detected_topic.get("clause_range"))
        parts = [q]
        if topic_bn:
            parts.append(topic_bn)
        if topic_en:
            parts.append(topic_en)
        if clause_range:
            parts.append(clause_range)
        return " | ".join([p for p in parts if p])
    return q


# =========================================================
# Qdrant search
# =========================================================
def _qdrant_search(collection: str, query_vector: List[float], *, top_k: int, qfilter: Optional[dict]) -> List[dict]:
    vector_name = _detect_vector_name(collection)

    if vector_name:
        payload: Dict[str, Any] = {
            "vector": {"name": vector_name, "vector": query_vector},
            "limit": int(top_k),
            "with_payload": True,
            "with_vector": False,
        }
    else:
        payload = {
            "vector": query_vector,
            "limit": int(top_k),
            "with_payload": True,
            "with_vector": False,
        }

    if qfilter:
        payload["filter"] = qfilter

    data = _http_json(
        "POST",
        f"{CFG.QDRANT_URL}/collections/{collection}/points/search",
        timeout=CFG.QDRANT_TIMEOUT,
        json_body=payload,
    )
    res = data.get("result") if isinstance(data, dict) else None
    return res if isinstance(res, list) else []


# =========================================================
# Context building
# =========================================================
def _extract_policy_lines(text: str) -> List[str]:
    raw_lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    pat_struct = re.compile(r"(^[-•])|(^[০-৯]+(\.[০-৯]+){0,4})|(^[0-9]+(\.[0-9]+){0,4})")
    structured = [ln for ln in raw_lines if pat_struct.search(ln)]
    return structured if structured else raw_lines


def _dedupe_keep_order(lines: List[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []

    for ln in lines:
        norm = _normalize_for_match(ln)
        if len(norm) < 4:
            continue
        if norm in seen:
            continue
        seen.add(norm)
        out.append(_norm_space(ln))

    return out


def _build_rag_context(hits: List[dict]) -> Dict[str, Any]:
    excerpts: List[str] = []
    citations: List[Dict[str, Any]] = []
    total_chars = 0
    idx = 0

    for h in hits[: CFG.MAX_EXCERPTS]:
        payload = h.get("payload") or {}
        txt = _clean(
            payload.get("text")
            or payload.get("content")
            or payload.get("chunk")
            or payload.get("prompt")
        )
        if not txt:
            continue

        topic_bn = _clean(payload.get("topic_bn"))
        topic_en = _clean(payload.get("topic_en"))
        clause = _clean(payload.get("clause_range"))
        src = _clean(payload.get("source_file") or payload.get("source"))

        lines = _dedupe_keep_order(_extract_policy_lines(txt))
        if not lines:
            continue

        lines = lines[: CFG.MAX_EXCERPT_LINES]

        idx += 1
        header = f"[{idx}] source_file={src} clause={clause} topic_bn={topic_bn} topic_en={topic_en}"
        block = header + "\n" + "\n".join(lines) + "\n"

        if total_chars + len(block) > CFG.MAX_CTX_CHARS:
            break

        excerpts.append(block)
        total_chars += len(block)
        citations.append(
            {
                "idx": idx,
                "id": payload.get("topic_id") or payload.get("id") or h.get("id"),
                "source_file": src,
                "clause": clause,
                "topic_bn": topic_bn,
                "topic_en": topic_en,
                "score": h.get("score"),
            }
        )

    return {"context": "\n".join(excerpts).strip(), "citations": citations}


# =========================================================
# Answer cleanup
# =========================================================
def _clean_llm_answer(ans: str, citations: List[Dict[str, Any]]) -> str:
    ans = (ans or "").strip()
    if not ans:
        return ""

    lines = [ln.strip() for ln in ans.splitlines() if ln.strip()]

    summary = next((ln for ln in lines if ln.startswith("Summary:")), "")
    if not summary:
        summary = (
            "Summary: নীতিমালার উদ্ধৃতি অনুযায়ী প্রাসঙ্গিক নিয়মগুলো সংক্ষেপে দেওয়া হলো।"
            if citations and any(_is_bn(c.get("topic_bn", "")) for c in citations)
            else "Summary: The relevant policy rules are summarized below from the retrieved excerpts."
        )

    bullets = [ln for ln in lines if ln.startswith("-")]
    bullets_clean: List[str] = []
    seen: set[str] = set()

    for b in bullets:
        nb = _normalize_for_match(b)
        if len(nb) < 6:
            continue
        if "source_file=" in b.lower() or "topic_bn=" in b.lower() or "topic_en=" in b.lower():
            continue
        if nb in seen:
            continue
        seen.add(nb)
        bullets_clean.append(b)

    out = [summary, "Rules:"]
    for b in bullets_clean[:8]:
        out.append(b)

    if not bullets_clean:
        out.append("- Policy does not state this clearly.")

    src = ", ".join([f"[{c['idx']}]" for c in citations[:3]]) if citations else "[1]"
    out.append(f"Sources: {src}")
    return "\n".join(out).strip()


def _build_messages(question_raw: str, ctx: str) -> List[Dict[str, str]]:
    user_prompt = f"""Question: {question_raw}
Answer language: {"Bangla" if _is_bn(question_raw) else "English"}

Use ONLY the excerpts below.

Excerpts:
{ctx}
"""
    return [
        {"role": "system", "content": SYSTEM_POLICY},
        {"role": "user", "content": user_prompt},
    ]


def _format_answer(question_raw: str, ctx: str, citations: List[Dict[str, Any]]) -> str:
    raw = _ollama_chat(_build_messages(question_raw, ctx))
    cleaned = _clean_llm_answer(raw, citations)
    return cleaned or ("Summary: Policy does not state this clearly.\nRules:\n- Policy does not state this clearly.\nSources: [1]")


# =========================================================
# Suggestions
# =========================================================
def _suggest_questions(topic: Optional[Dict[str, str]], question: str) -> List[str]:
    topic_id = _clean((topic or {}).get("topic_id"))
    topic_bn = _clean((topic or {}).get("topic_bn"))
    topic_en = _clean((topic or {}).get("topic_en"))

    if "6_8" in topic_id or topic_bn == "দূরত্ব ভাতা":
        return [
            "দূরত্ব ভাতা কোন গ্রেডের কর্মীরা পাবে?",
            "নিজস্ব বাসস্থান থেকে কত কিলোমিটার দূরে হলে দূরত্ব ভাতা প্রযোজ্য?",
            "দূরত্ব ভাতার হার কত?",
        ]
    if "3_6_3_5" in topic_id or "Sexual Harassment" in topic_en:
        return [
            "যৌন হয়রানির ক্ষেত্রে কী কী শাস্তি হতে পারে?",
            "মৌখিক তিরস্কার থেকে চাকুরীচ্যুতি পর্যন্ত ধাপগুলো কী?",
            "যৌন হয়রানির অভিযোগ প্রমাণিত হলে কী ব্যবস্থা নেওয়া হবে?",
        ]
    if topic_bn == "কর্মীদের শ্রেণী বিভাগ" or "1_1_" in topic_id:
        return [
            "নিয়মিত কর্মী বলতে কী বোঝায়?",
            "চুক্তিভিত্তিক কর্মী ও প্রকল্প কর্মীর পার্থক্য কী?",
            "সেবা বা খন্ডকালীন কর্মী কারা?",
        ]

    if _is_bn(question):
        return [
            "প্রশ্নটি আরেকটু নির্দিষ্ট করে লিখুন।",
            "উদাহরণ: কে প্রযোজ্য, হার কত, সময়সীমা কত, বা শর্ত কী?",
            "উদাহরণ: ‘দূরত্ব ভাতা কারা পাবে?’",
        ]

    return [
        "Please make the question more specific.",
        "Example: who is eligible, what is the rate, what is the time limit, or what are the conditions?",
        "Example: Who gets Distance Allowance?",
    ]


# =========================================================
# Core retrieval pipeline
# =========================================================
def _run_policy_pipeline(question_raw: str, collection: str, top_k: int) -> Dict[str, Any]:
    detected_topic = _detect_best_topic(question_raw, collection)
    qfilter = _build_exact_topic_filter(detected_topic)

    q_for_embed = _rewrite_query(question_raw, detected_topic)
    qvec = _ollama_embed(q_for_embed)

    hits = _qdrant_search(collection, qvec, top_k=top_k, qfilter=qfilter)

    if not detected_topic and not hits:
        hits = _qdrant_search(collection, qvec, top_k=max(top_k, 10), qfilter=None)

    filtered_hits: List[dict] = []
    for h in hits:
        try:
            score = float(h.get("score") or 0.0)
            if score >= float(CFG.SCORE_THRESHOLD):
                filtered_hits.append(h)
        except Exception:
            pass

    if filtered_hits:
        hits = filtered_hits

    built = _build_rag_context(hits)
    ctx = built["context"]
    citations = built["citations"]

    return {
        "detected_topic": detected_topic,
        "filter_used": qfilter,
        "rewritten_query": q_for_embed,
        "hits": hits,
        "context": ctx,
        "citations": citations,
    }


# =========================================================
# Main API - JSON
# =========================================================
@csrf_exempt
@require_POST
def policy_ask(request):
    body = _json_body(request)
    question_raw = _clean(body.get("question") or body.get("message"))
    if not question_raw:
        return JsonResponse({"ok": False, "error": "question is required"}, status=400)

    collection = _clean(body.get("collection")) or CFG.POLICY_COLLECTION
    top_k = int(body.get("top_k") or CFG.TOP_K)
    use_chat = int(body.get("use_chat") if body.get("use_chat") is not None else CFG.USE_CHAT_ANSWER)

    try:
        result = _run_policy_pipeline(question_raw, collection, top_k)

        detected_topic = result["detected_topic"]
        qfilter = result["filter_used"]
        q_for_embed = result["rewritten_query"]
        hits = result["hits"]
        ctx = result["context"]
        citations = result["citations"]

        if detected_topic and not hits:
            return JsonResponse(
                {
                    "ok": True,
                    "needs_clarification": True,
                    "topic_detected": detected_topic,
                    "answer": (
                        "প্রাসঙ্গিক টপিকের সঠিক উদ্ধৃতি পাওয়া যায়নি। collection-এর topic payload পরীক্ষা করুন।"
                        if _is_bn(question_raw)
                        else "A clear excerpt for the detected topic was not found. Please check the topic payload in the collection."
                    ),
                    "suggested_questions": _suggest_questions(detected_topic, question_raw),
                    "citations": [],
                    "matches": [],
                    "debug": {
                        "detected_topic": detected_topic,
                        "filter_used": qfilter,
                        "rewritten_query": q_for_embed,
                        "reason": "strict_topic_no_match",
                    },
                }
            )

        if detected_topic and not ctx.strip():
            return JsonResponse(
                {
                    "ok": True,
                    "needs_clarification": True,
                    "topic_detected": detected_topic,
                    "answer": (
                        "প্রাসঙ্গিক টপিকের উদ্ধৃতি পাওয়া যায়নি। টপিকের text/chunk payload পরীক্ষা করুন।"
                        if _is_bn(question_raw)
                        else "The detected topic was found but no usable excerpt was built. Please check the text/chunk payload."
                    ),
                    "suggested_questions": _suggest_questions(detected_topic, question_raw),
                    "citations": [],
                    "matches": [
                        {"id": h.get("id"), "score": h.get("score"), "payload": h.get("payload")}
                        for h in hits[:5]
                    ],
                    "debug": {
                        "detected_topic": detected_topic,
                        "filter_used": qfilter,
                        "rewritten_query": q_for_embed,
                        "reason": "topic_detected_but_empty_context",
                    },
                }
            )

        if not ctx.strip():
            return JsonResponse(
                {
                    "ok": True,
                    "needs_clarification": True,
                    "topic_detected": detected_topic,
                    "answer": (
                        "প্রাসঙ্গিক নীতিমালার তথ্য স্পষ্টভাবে পাওয়া যায়নি।"
                        if _is_bn(question_raw)
                        else "A clear policy excerpt was not found."
                    ),
                    "suggested_questions": _suggest_questions(detected_topic, question_raw),
                    "citations": [],
                    "matches": [],
                    "debug": {
                        "detected_topic": detected_topic,
                        "filter_used": qfilter,
                        "rewritten_query": q_for_embed,
                        "reason": "empty_context",
                    },
                }
            )

        if use_chat == 1:
            answer = _format_answer(question_raw, ctx, citations)
        else:
            answer = ctx[:1500]

        return JsonResponse(
            {
                "ok": True,
                "needs_clarification": False,
                "collection": collection,
                "top_k": top_k,
                "topic_detected": detected_topic,
                "answer": answer,
                "suggested_questions": [],
                "citations": citations,
                "matches": [
                    {"id": h.get("id"), "score": h.get("score"), "payload": h.get("payload")}
                    for h in hits[:10]
                ],
                "debug": {
                    "detected_topic": detected_topic,
                    "filter_used": qfilter,
                    "rewritten_query": q_for_embed,
                    "hits_count": len(hits),
                    "context_chars": len(ctx),
                    "use_chat": use_chat,
                    "chat_model": CFG.CHAT_MODEL,
                },
            }
        )

    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Qdrant search error: {e}"}, status=502)


# =========================================================
# Main API - Streaming
# NDJSON events:
#   {"type":"meta", ...}
#   {"type":"token","content":"..."}
#   {"type":"done"}
#   {"type":"final","answer":"..."}   # optional fallback
#   {"type":"error","message":"..."}
# =========================================================
@csrf_exempt
@require_POST
def policy_ask_stream(request):
    body = _json_body(request)
    question_raw = _clean(body.get("question") or body.get("message"))
    if not question_raw:
        return JsonResponse({"ok": False, "error": "question is required"}, status=400)

    collection = _clean(body.get("collection")) or CFG.POLICY_COLLECTION
    top_k = int(body.get("top_k") or CFG.TOP_K)
    stream_clean_final = int(body.get("stream_clean_final") or 0)

    try:
        result = _run_policy_pipeline(question_raw, collection, top_k)

        detected_topic = result["detected_topic"]
        qfilter = result["filter_used"]
        q_for_embed = result["rewritten_query"]
        hits = result["hits"]
        ctx = result["context"]
        citations = result["citations"]

        if detected_topic and not hits:
            msg = (
                "প্রাসঙ্গিক টপিকের সঠিক উদ্ধৃতি পাওয়া যায়নি। collection-এর topic payload পরীক্ষা করুন।"
                if _is_bn(question_raw)
                else "A clear excerpt for the detected topic was not found. Please check the topic payload in the collection."
            )
            return StreamingHttpResponse(
                [_ndjson({"type": "final", "answer": msg})],
                content_type="application/x-ndjson; charset=utf-8",
            )

        if detected_topic and not ctx.strip():
            msg = (
                "প্রাসঙ্গিক টপিকের উদ্ধৃতি পাওয়া যায়নি। টপিকের text/chunk payload পরীক্ষা করুন।"
                if _is_bn(question_raw)
                else "The detected topic was found but no usable excerpt was built. Please check the text/chunk payload."
            )
            return StreamingHttpResponse(
                [_ndjson({"type": "final", "answer": msg})],
                content_type="application/x-ndjson; charset=utf-8",
            )

        if not ctx.strip():
            msg = (
                "প্রাসঙ্গিক নীতিমালার তথ্য স্পষ্টভাবে পাওয়া যায়নি।"
                if _is_bn(question_raw)
                else "A clear policy excerpt was not found."
            )
            return StreamingHttpResponse(
                [_ndjson({"type": "final", "answer": msg})],
                content_type="application/x-ndjson; charset=utf-8",
            )

        def event_stream():
            yield _ndjson(
                {
                    "type": "meta",
                    "ok": True,
                    "topic_detected": detected_topic,
                    "citations": citations,
                    "debug": {
                        "detected_topic": detected_topic,
                        "filter_used": qfilter,
                        "rewritten_query": q_for_embed,
                        "hits_count": len(hits),
                        "context_chars": len(ctx),
                        "chat_model": CFG.CHAT_MODEL,
                    },
                }
            )

            if stream_clean_final == 1:
                raw_parts: List[str] = []
                for chunk in _ollama_chat_stream(_build_messages(question_raw, ctx)):
                    yield chunk
                    try:
                        obj = json.loads(chunk.strip())
                        if obj.get("type") == "token":
                            raw_parts.append(obj.get("content", ""))
                    except Exception:
                        pass

                full_raw = "".join(raw_parts)
                cleaned = _clean_llm_answer(full_raw, citations)
                if cleaned:
                    yield _ndjson({"type": "final_cleaned", "answer": cleaned})
            else:
                for chunk in _ollama_chat_stream(_build_messages(question_raw, ctx)):
                    yield chunk

        resp = StreamingHttpResponse(
            event_stream(),
            content_type="application/x-ndjson; charset=utf-8",
        )
        resp["Cache-Control"] = "no-cache"
        resp["X-Accel-Buffering"] = "no"
        return resp

    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Qdrant search error: {e}"}, status=502)
