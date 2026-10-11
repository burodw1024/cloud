from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union

import requests
from django.http import JsonResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

JsonT = Union[Dict[str, Any], List[Any]]
HandlerFn = Callable[[str, str, List[Dict[str, Any]]], Optional[str]]


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
    CHAT_NUM_PREDICT: int = _env_int("CHAT_NUM_PREDICT", "1200")
    CHAT_NUM_CTX: int = _env_int("CHAT_NUM_CTX", "4096")
    CHAT_KEEP_ALIVE: str = _env("CHAT_KEEP_ALIVE", "10m")

    # Retrieval
    TOP_K: int = _env_int("POLICY_TOP_K", "8")
    SCORE_THRESHOLD: float = _env_float("POLICY_SCORE_THRESHOLD", "0.22")

    # Context
    MAX_CTX_CHARS: int = _env_int("POLICY_MAX_CTX_CHARS", "20000")
    MAX_EXCERPT_LINES: int = _env_int("POLICY_MAX_EXCERPT_LINES", "80")
    MAX_EXCERPTS: int = _env_int("POLICY_MAX_EXCERPTS", "8")
    FULL_MAX_CTX_CHARS: int = _env_int("POLICY_FULL_MAX_CTX_CHARS", "40000")
    FULL_MAX_EXCERPT_LINES: int = _env_int("POLICY_FULL_MAX_EXCERPT_LINES", "150")
    FULL_MAX_EXCERPTS: int = _env_int("POLICY_FULL_MAX_EXCERPTS", "12")

    # Default behavior
    USE_CHAT_ANSWER: int = _env_int("POLICY_USE_CHAT_ANSWER", "1")

    # Humanized rewrite
    REWRITE_USER_QUERY: int = _env_int("POLICY_REWRITE_USER_QUERY", "1")
    REWRITE_TIMEOUT: float = _env_float("POLICY_REWRITE_TIMEOUT", "60")


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


def _bn_to_en_digits(text: str) -> str:
    return (text or "").translate(str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789"))


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
    s = s.replace("allowances", "allowance")

    s = s.replace("চাকুরী", "চাকরি")
    s = s.replace("ছুটী", "ছুটি")
    s = s.replace("বদলী", "বদলি")
    s = s.replace("হারাসমেন্ট", "হয়রানি")
    s = s.replace("harasment", "harassment")

    s = s.replace("over time", "overtime")
    s = s.replace("ot allowance", "ot")
    s = s.replace("food & lodging", "food lodging")
    s = s.replace("food and lodging", "food lodging")
    s = s.replace("per diem", "daily allowance")
    s = s.replace("distant allowance", "distance allowance")
    s = s.replace("distance allownace", "distance allowance")
    s = s.replace("distanse allowance", "distance allowance")

    # Common typos
    s = s.replace("rade structure", "grade structure")
    s = s.replace("grede structure", "grade structure")
    s = s.replace("grad structure", "grade structure")
    s = s.replace("grade stracture", "grade structure")

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


def _safe_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except Exception:
        return default


# =========================================================
# Follow-up helpers
# =========================================================
FOLLOWUP_TERMS = {
    "more", "details", "detail", "full", "complete", "show all", "all info",
    "all information", "elaborate", "expand", "explain more",
    "আরও", "বিস্তারিত", "আরো", "সব", "সব তথ্য", "সম্পূর্ণ", "পুরোটা",
    "পুরো তথ্য", "পুরো নিয়ম", "বিস্তারিত বলুন", "সব দেখাও", "আরও বলুন"
}


def _normalize_followup_text(text: str) -> str:
    return _normalize_for_match(text or "")


def _is_followup_request(question: str) -> bool:
    q = _normalize_followup_text(question)
    if not q:
        return False

    if q in FOLLOWUP_TERMS:
        return True

    patterns = [
        r"^tell me more$",
        r"^give me more$",
        r"^give full details$",
        r"^show all$",
        r"^show full$",
        r"^more details$",
        r"^full details$",
        r"^আরও$",
        r"^বিস্তারিত$",
        r"^সব তথ্য$",
        r"^সম্পূর্ণ$",
        r"^পুরোটা$",
        r"^বিস্তারিত বলুন$",
    ]
    return any(re.match(p, q) for p in patterns)


def _session_get_last_topic(request) -> Optional[Dict[str, str]]:
    try:
        topic = request.session.get("policy_last_topic")
        return topic if isinstance(topic, dict) else None
    except Exception:
        return None


def _session_set_last_topic(request, topic: Optional[Dict[str, str]]) -> None:
    try:
        if topic:
            request.session["policy_last_topic"] = {
                "topic_id": _clean(topic.get("topic_id")),
                "topic_bn": _clean(topic.get("topic_bn")),
                "topic_en": _clean(topic.get("topic_en")),
                "clause_range": _clean(topic.get("clause_range")),
                "source_file": _clean(topic.get("source_file")),
                "manual_key": _clean(topic.get("manual_key")),
            }
            request.session.modified = True
    except Exception:
        pass


def _session_clear_last_topic(request) -> None:
    try:
        if "policy_last_topic" in request.session:
            del request.session["policy_last_topic"]
            request.session.modified = True
    except Exception:
        pass


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


SYSTEM_POLICY_SUMMARY = """You are a professional HR policy assistant for the organization.

Your job:
- Understand what the user is actually asking for.
- Answer like an experienced HR manager would explain it.
- Use ONLY the retrieved policy excerpts as the source of truth.
- Never invent facts not present in the policy.
- If the user asks for a count, give only the count first.
- If the user asks for a list, provide the full list.
- If the user asks for explanation, explain simply in human language.
- If the user asks for eligibility, rate, authority, process, exception, or comparison, answer in that exact style.
- Keep the answer short, natural, and clear.
- If the question is in Bangla, answer in Bangla.

Format EXACTLY:
Summary: <1-3 lines>
Rules:
- <short bullet>
- <short bullet>
Sources: [1], [2]...
"""


SYSTEM_POLICY_FULL = """You are a professional HR policy assistant for the organization.

Your job:
- Understand what the user is actually asking for.
- Answer like an experienced HR manager would explain it.
- Use ONLY the retrieved policy excerpts as the source of truth.
- Never invent facts not present in the policy.
- Include all relevant conditions, exceptions, authorities, rates, and procedural points visible in the excerpts.
- Keep the tone natural and clear.
- If the question is in Bangla, answer in Bangla.

Format EXACTLY:
Summary: <2-4 lines>
Rules:
- <complete bullet>
- <complete bullet>
- <complete bullet>
Sources: [1], [2]...
"""


def _ollama_chat(messages: List[Dict[str, str]], *, timeout: Optional[float] = None) -> str:
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
            timeout=float(timeout or CFG.CHAT_TIMEOUT),
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
                yield _ndjson({"type": "error", "message": f"Ollama HTTP {r.status_code}: {r.text[:1000]}"})
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
# Humanized question rewrite
# =========================================================
def _should_humanize_rewrite(question_raw: str) -> bool:
    q = _clean(question_raw)
    if not q:
        return False

    token_count = len(q.split())

    if token_count <= 2:
        return False

    if token_count <= 5 and ("?" in q or q.endswith("?")):
        return False

    if len(q) > 260:
        return False

    return True


def _fallback_humanized_question(question_raw: str) -> str:
    raw = _norm_space(question_raw)
    if not raw:
        return raw

    q = raw.rstrip("?.! ")

    if _is_bn(raw):
        return q + " বিষয়ে নীতিমালায় কী বলা আছে?"
    return f"What does the HR policy say about {q}?"


def _rewrite_user_question_humanized(question_raw: str) -> str:
    raw = _clean(question_raw)
    if not raw:
        return raw

    normalized = _norm_space(raw)
    if CFG.REWRITE_USER_QUERY != 1:
        return normalized

    if not _should_humanize_rewrite(normalized):
        return normalized

    system = """You rewrite user questions for HR policy retrieval.

Rules:
- Preserve the exact meaning.
- Do not answer the question.
- Do not add facts or assumptions.
- Do not remove important terms like grade, allowance type, leave type, notice, overtime, clause references, or job title.
- Fix grammar, wording, and readability.
- Make it short, clear, and human-readable.
- If the input is Bangla, return Bangla.
- If the input is English, return English.
- If the input is mixed Bangla-English, preserve important policy terms.
- Return ONLY the rewritten question text.
"""

    user = f"Rewrite this user question for policy retrieval:\n{normalized}"

    rewritten = _ollama_chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        timeout=CFG.REWRITE_TIMEOUT,
    ).strip()

    if not rewritten:
        return _fallback_humanized_question(normalized)

    rewritten = _norm_space(rewritten)
    if not rewritten:
        return _fallback_humanized_question(normalized)

    if len(rewritten) > max(220, len(normalized) * 3):
        return _fallback_humanized_question(normalized)

    return rewritten


# =========================================================
# Manual intent aliases
# =========================================================
INTENT_ALIASES: Dict[str, List[str]] = {
    "staff_classification": [
        "types of staff", "type of staff", "staff type", "staff classification",
        "classification of staff", "employee type", "employee classification",
        "কর্মীদের শ্রেণী বিভাগ", "কর্মীদের ধরন", "কর্মীর ধরন", "কর্মীদের প্রকারভেদ",
        "স্টাফের ধরন", "স্টাফ টাইপ",
    ],
    "departments": [
        "departments of the organization", "organization departments", "department list",
        "সংস্থার বিভাগসমূহ", "বিভাগসমূহ", "বিভাগের তালিকা",
    ],
    "grade_structure": [
        "grade structure", "grades", "posts and grades", "grade list", "grade wise post",
        "grade wise designation", "grade 1 to 16", "executive director to support staff",
        "গ্রেড কাঠামো", "গ্রেড", "পদ ও গ্রেড", "গ্রেডভিত্তিক পদ", "গ্রেড অনুযায়ী পদ",
        "গ্রেড ১ থেকে ১৬", "নির্বাহী পরিচালক থেকে সাপোর্ট স্টাফ",
        "rade structure", "grede structure", "grad structure", "grade stracture", "গ্রেড স্ট্রাকচার",
    ],
    "educational_qualification": [
        "educational qualification",
        "minimum educational qualification",
        "qualification by grade",
        "grade wise qualification",
        "grade-wise qualification",
        "employee educational qualification",
        "education requirement by grade",
        "what qualification is needed for grade",
        "which grades require postgraduate",
        "who determines educational qualification",
        "driver qualification relaxation",
        "কর্মীদের পদ বিন্যাস অনুযায়ী শিক্ষাগত যোগ্যতা",
        "শিক্ষাগত যোগ্যতা",
        "ন্যূনতম শিক্ষাগত যোগ্যতা",
        "পদ অনুযায়ী শিক্ষাগত যোগ্যতা",
        "গ্রেড অনুযায়ী শিক্ষাগত যোগ্যতা",
        "গ্রেডভিত্তিক শিক্ষাগত যোগ্যতা",
        "গ্রেড ২ থেকে ১১ শিক্ষাগত যোগ্যতা",
        "গ্রেড-২ থেকে গ্রেড-১১ শিক্ষাগত যোগ্যতা",
        "গ্রেড ১২ শিক্ষাগত যোগ্যতা",
        "গ্রেড-১২ শিক্ষাগত যোগ্যতা",
        "গ্রেড ১৩ শিক্ষাগত যোগ্যতা",
        "গ্রেড-১৩ শিক্ষাগত যোগ্যতা",
        "গ্রেড ১৪ শিক্ষাগত যোগ্যতা",
        "গ্রেড-১৪ শিক্ষাগত যোগ্যতা",
        "গ্রেড ১৫ শিক্ষাগত যোগ্যতা",
        "গ্রেড-১৫ শিক্ষাগত যোগ্যতা",
        "গ্রেড ১৬ শিক্ষাগত যোগ্যতা",
        "গ্রেড-১৬ শিক্ষাগত যোগ্যতা",
        "স্নাতকোত্তর কোন গ্রেডে লাগবে",
        "স্নাতক কোন গ্রেডে লাগবে",
        "উচ্চ মাধ্যমিক কোন গ্রেডে লাগবে",
        "মাধ্যমিক কোন গ্রেডে লাগবে",
        "গাড়ি চালকদের শিক্ষাগত যোগ্যতা",
        "ড্রাইভারের শিক্ষাগত যোগ্যতা",
        "শিথিল করা যাবে",
        "মানব সম্পদ ব্যবস্থাপনা বিভাগ নির্ধারণ করবে",
        "গভর্ণিং বডি নির্ধারণ করবে",
    ],
    "recruitment_advertisement": ["recruitment advertisement", "job circular", "নিয়োগ বিজ্ঞপ্তি", "নিয়োগ সার্কুলার"],
    "age_limit_recruitment": ["age limit for recruitment", "recruitment age limit", "age limit", "নিয়োগের বয়সসীমা", "বয়সসীমা"],
    "recruitment_board": ["recruitment board", "selection board", "নিয়োগ বোর্ড", "নিয়োগ কমিটি"],
    "joining_rules": ["joining rules", "rules of joining", "joining policy", "যোগদান বিধি", "যোগদানের নিয়ম", "চাকরিতে যোগদান"],
    "basic_transfer_policy": [
        "transfer policy", "basic transfer policy", "employee transfer policy",
        "বদলি নীতি", "বদলির নীতি", "বদলির মৌলিক নীতি", "বদলী নীতি",
    ],
    "transfer_notice_period": ["transfer notice period", "notice before transfer", "বদলির নোটিশ", "বদলীর নোটিশ", "বদলির নোটিশ সময়"],
    "transfer_tenure_rules": ["transfer tenure", "how long before transfer", "বদলির সময়কাল", "কত বছর পরে বদলি"],
    "attendance_recording": [
        "attendance", "attendance policy", "attendance rules", "attendance system",
        "attendance recording", "attendance record", "office attendance", "হাজিরা",
        "হাজিরা নীতি", "হাজিরা রেকর্ড", "উপস্থিতি", "উপস্থিতির রেকর্ড", "উপস্থিতি নীতি",
    ],
    "late_attendance_absence": [
        "late attendance", "absence rule", "late and absence", "late attendance and absence",
        "attendance late", "বিলম্বে উপস্থিতি", "অনুপস্থিতি", "দেরিতে আসা", "বিলম্বে উপস্থিতি ও অনুপস্থিতি",
    ],
    "probation_confirmation": ["probation", "confirmation", "probation and confirmation", "শিক্ষানবিশকাল", "স্থায়ীকরণ", "শিক্ষানবিশকাল ও স্থায়ীকরণ"],
    "seniority_register_basis": ["seniority", "seniority basis", "জ্যেষ্ঠতা", "জ্যেষ্ঠতার ভিত্তি"],
    "promotion_basic": ["promotion policy", "basic promotion principles", "পদোন্নতির নীতি", "পদোন্নতির মৌলিক নীতি", "promotion principles"],
    "promotion_eligibility": [
        "promotion eligibility", "promotion service period", "eligibility for promotion",
        "who is eligible for promotion", "promotion qualification", "grade wise promotion eligibility",
        "পদোন্নতির যোগ্যতা", "পদোন্নতির সময়সীমা", "পদোন্নতির শর্ত", "পদোন্নতির জন্য কী লাগবে",
        "আমি গ্রেড ১২ এ আছি আমার পদোন্নতির যোগ্যতা কী", "গ্রেড ১২ পদোন্নতি", "গ্রেড-১২ পদোন্নতি", "গ্রেড ১২ এ আছি পদোন্নতি",
    ],
    "annual_appraisal": ["performance appraisal", "annual appraisal", "বার্ষিক কর্মমূল্যায়ন", "কর্মমূল্যায়ন"],
    "sexual_harassment_prevention": ["sexual harassment prevention", "sexual harassment", "sexual harasment", "harassment policy", "যৌন হয়রানি প্রতিরোধ", "যৌন হয়রানি"],
    "sexual_harassment_penalty": [
        "sexual harassment penalty", "sexual harassment punishment", "penalty scale for sexual harassment",
        "sexual harasment penalty", "যৌন হয়রানির শাস্তির ধাপ", "যৌন হয়রানির শাস্তি",
    ],
    "show_cause_notice": ["show cause notice", "কারণ দর্শানো নোটিশ", "শোকজ"],
    "warning_letter": ["warning letter", "সতর্কীকরণ পত্র"],
    "distance_allowance": [
        "distance allowance", "allowance for distance", "distant allowance", "distance allownace",
        "distanse allowance", "travel distance allowance", "far distance allowance", "দূরত্ব ভাতা",
        "দুরত্ব ভাতা", "দূর ভাতা", "স্থায়ী ঠিকানা থেকে কর্মস্থলের দূরত্ব", "কিলোমিটার অনুযায়ী ভাতা",
    ],
    "daily_travel_allowance": ["daily travel allowance", "travel allowance", "transport allowance", "daily transport allowance", "দৈনন্দিন যাতায়াত ভাতা", "যাতায়াত ভাতা"],
    "food_lodging_daily": [
        "food lodging and daily allowance", "food, lodging and daily allowance", "daily allowance",
        "food allowance", "lodging allowance", "accommodation allowance", "meal allowance",
        "daily subsistence allowance", "খাদ্য অবস্থান ও দৈনিক ভাতা", "খাদ্য, অবস্থান ও দৈনিক ভাতা",
        "দৈনিক ভাতা", "খাদ্যভাতা", "খাদ্য ভাতা", "অবস্থান ভাতা", "মেস",
    ],
    "overtime_allowance": [
        "overtime allowance", "over time allowance", "overtime", "over time", "ot allowance", "ot",
        "অধিকাল ভাতা", "ওভারটাইম", "অতিরিক্ত সময়ের ভাতা", "অতিরিক্ত সময়ের ভাতা",
    ],
    "transfer_allowance": ["transfer allowance", "বদলি ভাতা", "স্থানান্তর ভাতা", "বদলি/স্থানান্তর ভাতা"],
    "salary_allowance_structure": ["salary and allowance structure", "salary structure", "বেতন ও ভাতা পরিকল্পনা", "বেতন কাঠামো"],
    "gratuity_policy": ["gratuity", "gratuity policy", "গ্র্যাচুইটি", "গ্র্যাচুইটি নীতি"],
    "provident_fund": ["provident fund", "pf", "প্রভিডেন্ট ফান্ড"],
    "income_tax": ["income tax liability", "income tax", "আয়কর", "আয়কর দায়"],
    "resignation_rules": ["resignation rules", "rules of resignation", "পদত্যাগের নিয়ম", "পদত্যাগ"],
    "retirement_normal": ["normal retirement", "retirement", "অবসর", "স্বাভাবিক অবসর"],
    "termination_general": ["termination", "grounds for termination", "অব্যাহতির কারণ", "অব্যাহতির সাধারণ কারণ"],
    "leave_general": ["general leave rules", "leave policy", "ছুটির সাধারণ নীতি", "ছুটির নীতি", "ছুটি"],
    "earned_leave": ["earned leave", "অর্জিত ছুটি"],
    "casual_leave": ["casual leave", "নৈমিত্তিক ছুটি"],
    "maternity_leave": ["maternity leave", "মাতৃত্বজনিত ছুটি"],
    "paternity_leave": ["paternity leave", "পিতৃত্বজনিত ছুটি"],
    "festival_leave": ["festival leave", "উৎসবকালীন ছুটি"],
    "sick_leave": ["sick leave", "অসুস্থতাজনিত ছুটি"],
    "higher_education_leave": ["higher education leave", "study leave", "উচ্চ শিক্ষা ছুটি"],
    "leave_authority": ["leave sanctioning authority", "who can approve leave", "ছুটি অনুমোদনকারী কর্তৃপক্ষ"],
}


MANUAL_TOPIC_RULES: List[Dict[str, str]] = [
    {"key": "staff_classification", "topic_id_prefix": "policy_1_1_", "topic_bn": "কর্মীদের শ্রেণী বিভাগ", "topic_en": "Staff Classification"},
    {"key": "departments", "topic_id": "policy_1_2_1_1_2_12", "topic_bn": "সংস্থার বিভাগসমূহ", "topic_en": "Departments of the Organization"},
    {"key": "grade_structure", "topic_id": "policy_1_3_1", "topic_bn": "গ্রেড কাঠামো", "topic_en": "Grade Structure"},
    {"key": "educational_qualification", "topic_id": "policy_1_4", "topic_bn": "কর্মীদের পদ বিন্যাস অনুযায়ী শিক্ষাগত যোগ্যতা", "topic_en": "Educational Qualifications by Employee Grade"},
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


ALLOWANCE_CANONICAL_QUESTIONS: Dict[str, str] = {
    "daily_travel_allowance": (
        "What does the HR policy say about daily travel allowance "
        "(দৈনন্দিন যাতায়াত ভাতা), including eligibility and applicable conditions?"
    ),
    "food_lodging_daily": (
        "What does the HR policy say about food, lodging, and daily allowance "
        "(খাদ্য, অবস্থান ও দৈনিক ভাতা), including mess, food allowance, lodging allowance, "
        "and location-based rates?"
    ),
    "overtime_allowance": (
        "What does the HR policy say about overtime allowance "
        "(অধিকাল ভাতা), including eligibility, work hours, holidays, and staff-specific rules?"
    ),
}


TOPIC_TEXT_CHECKS: Dict[str, List[str]] = {
    "grade_structure": [
        "গ্রেড-", "grade-", "grade ", "নির্বাহী পরিচালক", "executive director",
        "সাপোর্ট স্টাফ", "support staff",
    ],
    "educational_qualification": [
        "শিক্ষাগত যোগ্যতা",
        "ন্যূনতম শিক্ষাগত যোগ্যতা",
        "স্নাতকোত্তর",
        "স্নাতক",
        "স্নাতক সম্মান",
        "উচ্চ মাধ্যমিক",
        "মাধ্যমিক",
        "গাড়ী চালক",
        "গাড়ি চালক",
        "driver",
        "governing body",
        "human resource management division",
        "educational qualification",
        "minimum qualification",
        "postgraduate",
        "graduate",
        "higher secondary",
        "secondary",
    ],
    "distance_allowance": ["দূরত্ব ভাতা", "distance allowance", "কিলোমিটার", "kilometer"],
    "daily_travel_allowance": [
        "daily travel allowance", "travel allowance", "transport allowance",
        "দৈনন্দিন যাতায়াত ভাতা", "যাতায়াত ভাতা",
    ],
    "food_lodging_daily": [
        "food lodging and daily allowance", "daily allowance", "food allowance",
        "lodging allowance", "accommodation allowance", "খাদ্য, অবস্থান ও দৈনিক ভাতা",
        "খাদ্য অবস্থান ও দৈনিক ভাতা", "দৈনিক ভাতা", "খাদ্যভাতা", "খাদ্য ভাতা",
        "অবস্থান ভাতা", "মেস",
    ],
    "overtime_allowance": [
        "overtime allowance", "over time allowance", "overtime", "অধিকাল ভাতা",
        "ওভারটাইম", "অতিরিক্ত সময়ের ভাতা", "অতিরিক্ত সময়ের ভাতা",
        "ঘণ্টা", "ছুটির দিন", "গাড়িচালক", "বাবুর্চি", "প্রহরী", "গ্রেড-১৬", "গ্রেড ১৬",
    ],
    "transfer_allowance": ["transfer allowance", "বদলি ভাতা", "স্থানান্তর ভাতা", "বদলি/স্থানান্তর ভাতা"],
}


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
    payload = {"limit": limit, "with_payload": True, "with_vector": False}
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


def _clear_topic_cache(collection: Optional[str] = None) -> None:
    global _TOPIC_CACHE
    if collection:
        _TOPIC_CACHE.pop(collection, None)
    else:
        _TOPIC_CACHE.clear()


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

    if "পদোন্নতি" in q or "promotion" in q:
        if "পদোন্নতি" in topic_bn or "promotion" in topic_en.lower():
            score += 100

    if "যোগ্যতা" in q or "eligibility" in q:
        if "যোগ্যতা" in topic_bn or "eligibility" in topic_en.lower():
            score += 80

    if "গ্রেড" in q and ("গ্রেড কাঠামো" in topic_bn or "grade structure" in topic_en.lower()):
        if "পদোন্নতি" not in q and "promotion" not in q:
            score += 40
        else:
            score -= 60

    if "attendance" in q or "হাজিরা" in q or "উপস্থিতি" in q:
        if "attendance" in topic_en.lower() or "হাজিরা" in topic_bn or "উপস্থিতি" in topic_bn:
            score += 110

    if "late attendance" in q or "absence" in q or "অনুপস্থিতি" in q or "বিলম্বে উপস্থিতি" in q:
        if "absence" in topic_en.lower() or "late attendance" in topic_en.lower() or "অনুপস্থিতি" in topic_bn:
            score += 100

    if "sexual harassment" in q or "যৌন হয়রানি" in q:
        if "sexual harassment" in topic_en.lower() or "যৌন হয়রানি" in topic_bn:
            score += 120

    overlap = q_tokens & hay_tokens
    score += len(overlap) * 8

    if q and q in hay:
        score += 60
    if hay and hay in q:
        score += 45

    return score


def _score_manual_rule(question: str, rule: Dict[str, str]) -> int:
    q = _normalize_for_match(question)
    key = _clean(rule.get("key"))
    aliases = INTENT_ALIASES.get(key, [])
    score = 0

    for alias in aliases:
        a = _normalize_for_match(alias)
        if not a:
            continue
        if q == a:
            score += 200
        elif a in q:
            score += 80
        else:
            q_tokens = set(q.split())
            a_tokens = set(a.split())
            overlap = q_tokens & a_tokens
            if overlap:
                score += len(overlap) * 10

    if key in ALLOWANCE_CANONICAL_QUESTIONS and ("allowance" in q or "ভাতা" in q):
        score += 15

    if key == "grade_structure":
        if any(x in q for x in ["গ্রেড", "grade", "নির্বাহী পরিচালক", "executive director", "সাপোর্ট স্টাফ", "support staff"]):
            score += 120
        if ("পদোন্নতি" in q or "promotion" in q) and score > 0:
            score -= 80

    if key == "educational_qualification":
        if any(
            x in q
            for x in [
                "শিক্ষাগত যোগ্যতা",
                "ন্যূনতম শিক্ষাগত যোগ্যতা",
                "স্নাতকোত্তর",
                "স্নাতক",
                "উচ্চ মাধ্যমিক",
                "মাধ্যমিক",
                "গাড়ি চালক",
                "গাড়ি চালক",
                "ড্রাইভার",
                "educational qualification",
                "minimum qualification",
                "postgraduate",
                "graduate",
                "higher secondary",
                "secondary",
                "driver",
                "governing body",
                "human resource management division",
            ]
        ):
            score += 130

        if any(x in q for x in ["গ্রেড", "grade"]):
            score += 40

        if "পদোন্নতি" in q or "promotion" in q:
            score -= 50

    if key == "distance_allowance":
        if any(x in q for x in ["distance", "distant", "দূরত্ব", "দুরত্ব", "কিলোমিটার"]):
            score += 120

    if key != "distance_allowance" and "allowance" in key:
        if any(x in q for x in ["distance", "distant", "দূরত্ব", "দুরত্ব", "কিলোমিটার"]):
            score -= 40

    if key == "food_lodging_daily":
        if any(x in q for x in ["food", "lodging", "খাদ্য", "অবস্থান", "মেস"]):
            score += 120

    return score


def _resolve_manual_topic(question: str) -> Optional[Dict[str, str]]:
    best_rule: Optional[Dict[str, str]] = None
    best_score = 0

    for rule in MANUAL_TOPIC_RULES:
        s = _score_manual_rule(question, rule)
        if s > best_score:
            best_score = s
            best_rule = rule

    if best_rule and best_score >= 20:
        return best_rule
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
                out = dict(t)
                out["manual_key"] = manual.get("key", "")
                return out
            if topic_bn and _clean(t.get("topic_bn")) == topic_bn:
                out = dict(t)
                out["manual_key"] = manual.get("key", "")
                return out
            if topic_en and _clean(t.get("topic_en")) == topic_en:
                out = dict(t)
                out["manual_key"] = manual.get("key", "")
                return out
            if topic_id_prefix and _clean(t.get("topic_id")).startswith(topic_id_prefix):
                out = dict(t)
                out["manual_key"] = manual.get("key", "")
                return out

        return {
            "topic_id": topic_id or topic_id_prefix,
            "topic_bn": topic_bn,
            "topic_en": topic_en,
            "clause_range": "",
            "source_file": "",
            "manual_key": manual.get("key", ""),
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


def _build_allowance_search_query(question: str, detected_topic: Optional[Dict[str, str]]) -> Optional[str]:
    if not detected_topic:
        return None

    manual_key = _clean(detected_topic.get("manual_key"))
    topic_bn = _clean(detected_topic.get("topic_bn"))
    topic_en = _clean(detected_topic.get("topic_en"))

    topic_key = manual_key
    if not topic_key:
        for rule in MANUAL_TOPIC_RULES:
            if (
                _clean(rule.get("topic_id")) == _clean(detected_topic.get("topic_id"))
                or _clean(rule.get("topic_bn")) == topic_bn
                or _clean(rule.get("topic_en")) == topic_en
            ):
                topic_key = _clean(rule.get("key"))
                break

    if topic_key not in ALLOWANCE_CANONICAL_QUESTIONS:
        return None

    parts: List[str] = []
    seen: set[str] = set()

    def add_part(value: str) -> None:
        v = _clean(value)
        k = _normalize_for_match(v)
        if v and k and k not in seen:
            seen.add(k)
            parts.append(v)

    add_part(ALLOWANCE_CANONICAL_QUESTIONS[topic_key])
    add_part(question)
    for alias in INTENT_ALIASES.get(topic_key, []):
        add_part(alias)
    add_part(topic_bn)
    add_part(topic_en)
    add_part(_clean(detected_topic.get("clause_range")))

    return " | ".join(parts) if parts else None


def _rewrite_query(question: str, detected_topic: Optional[Dict[str, str]]) -> str:
    q = _clean(question)

    grade_match = re.search(r"গ্রেড[-\s]*([০-৯0-9]+)|grade[-\s]*([0-9]+)", q, re.I)
    grade_text = ""
    if grade_match:
        grade_text = grade_match.group(0)

    allowance_rewrite = _build_allowance_search_query(q, detected_topic)
    if allowance_rewrite:
        return allowance_rewrite

    if detected_topic:
        topic_bn = _clean(detected_topic.get("topic_bn"))
        topic_en = _clean(detected_topic.get("topic_en"))
        clause_range = _clean(detected_topic.get("clause_range"))

        parts = [q]

        if "পদোন্নতি" in q or "promotion" in q.lower():
            parts.append("পদোন্নতির যোগ্যতা")
            parts.append("promotion eligibility")

        if "attendance" in q.lower() or "হাজিরা" in q or "উপস্থিতি" in q:
            parts.append("attendance policy")
            parts.append("হাজিরা রেকর্ড")
            parts.append("বিলম্বে উপস্থিতি ও অনুপস্থিতি")

        if grade_text:
            parts.append(grade_text)

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
    pat_struct = re.compile(r"(^[-•])|(^[০-৯]+(\.[০-৯]+){0,4})|(^[0-9]+(\.[0-9]+){0,4})|(^গ্রেড[-\s]*[০-৯0-9]+)")
    structured = [ln for ln in raw_lines if pat_struct.search(ln)]
    return structured if structured else raw_lines


def _dedupe_keep_order(lines: List[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []

    for ln in lines:
        norm = _normalize_for_match(ln)
        if len(norm) < 8:
            continue
        if len(ln) < 12:
            continue
        if norm in seen:
            continue
        seen.add(norm)
        out.append(_norm_space(ln))

    return out


def _build_rag_context(hits: List[dict], mode: str = "summary") -> Dict[str, Any]:
    excerpts: List[str] = []
    citations: List[Dict[str, Any]] = []
    total_chars = 0
    idx = 0

    max_excerpts = CFG.MAX_EXCERPTS if mode == "summary" else CFG.FULL_MAX_EXCERPTS
    max_lines = CFG.MAX_EXCERPT_LINES if mode == "summary" else CFG.FULL_MAX_EXCERPT_LINES
    max_chars = CFG.MAX_CTX_CHARS if mode == "summary" else CFG.FULL_MAX_CTX_CHARS

    for h in hits[:max_excerpts]:
        payload = h.get("payload") or {}
        txt = _clean(payload.get("text") or payload.get("content") or payload.get("chunk") or payload.get("prompt"))
        if not txt:
            continue

        topic_bn = _clean(payload.get("topic_bn"))
        topic_en = _clean(payload.get("topic_en"))
        clause = _clean(payload.get("clause_range"))
        src = _clean(payload.get("source_file") or payload.get("source"))

        lines = _dedupe_keep_order(_extract_policy_lines(txt))
        lines = [ln for ln in lines if len(_normalize_for_match(ln)) >= 8]
        if not lines:
            continue

        lines = lines[:max_lines]

        idx += 1
        header = f"[{idx}] source_file={src} clause={clause} topic_bn={topic_bn} topic_en={topic_en}"
        block = header + "\n" + "\n".join(lines) + "\n"

        if total_chars + len(block) > max_chars:
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
# Intent detection and reusable deterministic helpers
# =========================================================
def _detect_answer_intent(question: str) -> str:
    q = _normalize_for_match(question)

    if any(x in q for x in ["how many", "number of", "count", "কতটি", "কয়টি", "মোট", "সংখ্যা"]):
        return "count"

    if any(x in q for x in ["highest grade", "top grade", "highest", "top", "সর্বোচ্চ গ্রেড", "উচ্চতম গ্রেড", "সর্বোচ্চ"]):
        return "highest"

    if any(x in q for x in ["lowest grade", "last grade", "lowest", "last", "সর্বনিম্ন গ্রেড", "নিম্নতম গ্রেড", "সর্বনিম্ন"]):
        return "lowest"

    if any(x in q for x in ["which post", "what post", "which designation", "what designation", "কোন পদ", "কি পদ", "কী পদ"]):
        return "lookup"

    if any(x in q for x in ["list", "show all", "full list", "তালিকা", "সবগুলো", "সব", "list all"]):
        return "list"

    if any(x in q for x in ["what is", "define", "meaning", "কি", "কী", "মানে", "অর্থ"]):
        return "definition"

    if any(x in q for x in ["who can", "eligible", "eligibility", "কারা", "যোগ্য", "প্রযোজ্য"]):
        return "eligibility"

    if any(x in q for x in ["how much", "rate", "amount", "কত", "হার", "পরিমাণ"]):
        return "rate"

    if any(x in q for x in ["who approves", "authority", "approved by", "কর্তৃপক্ষ", "অনুমোদন", "কে অনুমোদন"]):
        return "authority"

    if any(x in q for x in ["how to", "process", "procedure", "steps", "কীভাবে", "প্রক্রিয়া", "ধাপ"]):
        return "procedure"

    if any(x in q for x in ["difference", "compare", "comparison", "পার্থক্য", "তুলনা"]):
        return "comparison"

    if any(x in q for x in ["exception", "except", "not applicable", "ব্যতিক্রম", "প্রযোজ্য নয়"]):
        return "exception"

    if any(x in q for x in ["yes or no", "can i", "is it allowed", "হবে কি", "পারবো কি", "যাবে কি"]):
        return "yes_no"

    return "explanation"


def _format_ctx_as_bullets(ctx: str) -> str:
    lines = [ln.strip() for ln in ctx.splitlines() if ln.strip()]
    return "- " + "\n- ".join(lines) if lines else "- Policy does not state this clearly."


def _extract_grade_number(question: str) -> Optional[str]:
    q = _bn_to_en_digits(_clean(question))
    m = re.search(r"(?:গ্রেড|grade)[-\s]*([0-9]{1,2})", q, re.I)
    return m.group(1) if m else None


def _extract_all_grade_numbers(question: str) -> List[str]:
    q = _bn_to_en_digits(_clean(question))
    matches = re.findall(r"(?:গ্রেড|grade)[-\s]*([0-9]{1,2})", q, re.I)
    out: List[str] = []
    seen: set[str] = set()
    for g in matches:
        gg = _clean(g)
        if gg and gg not in seen:
            seen.add(gg)
            out.append(gg)
    return out


def _is_grade_lookup_question(question: str) -> bool:
    q = _normalize_for_match(question)
    return (
        ("গ্রেড" in q or "grade" in q)
        and any(x in q for x in ["কোন পদ", "কি পদ", "কী পদ", "which post", "what post", "which designation", "what designation"])
    )


def _is_education_question(question: str) -> bool:
    q = _normalize_for_match(question)
    return any(
        x in q
        for x in [
            "শিক্ষাগত যোগ্যতা",
            "ন্যূনতম শিক্ষাগত যোগ্যতা",
            "যোগ্যতা",
            "educational qualification",
            "minimum qualification",
            "qualification",
            "postgraduate",
            "graduate",
            "higher secondary",
            "secondary",
            "স্নাতকোত্তর",
            "স্নাতক",
            "উচ্চ মাধ্যমিক",
            "মাধ্যমিক",
        ]
    )


def _find_grade_line(ctx: str, grade_no: str) -> Optional[str]:
    if not ctx.strip() or not grade_no:
        return None

    patterns = [
        rf"^গ্রেড[-\s]*{grade_no}\s*:\s*(.+)$",
        rf"^grade[-\s]*{grade_no}\s*:\s*(.+)$",
    ]

    for raw_line in ctx.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        line2 = _bn_to_en_digits(line)
        for p in patterns:
            if re.match(p, line2, re.I):
                return line

    return None


def _find_first_grade_line(ctx: str) -> Optional[str]:
    return _find_grade_line(ctx, "1")


def _find_last_grade_line(ctx: str) -> Optional[str]:
    return _find_grade_line(ctx, "16")


def _find_first_matching_line(ctx: str, patterns: List[str]) -> Optional[str]:
    if not ctx.strip():
        return None

    for raw_line in ctx.splitlines():
        line = raw_line.strip()
        norm = _normalize_for_match(line)
        if any(p in norm for p in patterns):
            return line
    return None


def _find_grade_related_lines(ctx: str, grade_no: str) -> List[str]:
    if not ctx.strip() or not grade_no:
        return []

    grade_no = _clean(grade_no)
    all_lines = [ln.strip() for ln in ctx.splitlines() if ln.strip()]
    out: List[str] = []

    for line in all_lines:
        line2 = _bn_to_en_digits(line)
        norm = _normalize_for_match(line2)

        if re.search(rf"(?:গ্রেড|grade)[-\s]*{re.escape(grade_no)}\b", line2, re.I):
            out.append(line)
            continue

        if grade_no == "1" and any(x in norm for x in ["governing body", "গভর্ণিং বডি"]):
            out.append(line)
            continue

        if grade_no in {"2", "3", "4", "5", "6", "7", "8", "9", "10", "11"}:
            if any(x in norm for x in ["গ্রেড-২ থেকে গ্রেড-১১", "গ্রেড ২ থেকে গ্রেড ১১", "grade-2 to grade-11", "grade 2 to grade 11"]):
                out.append(line)
                continue

        if grade_no == "12" and re.search(r"(?:গ্রেড|grade)[-\s]*12\b", line2, re.I):
            out.append(line)
            continue

        if grade_no in {"13", "14"} and (
            re.search(r"(?:গ্রেড|grade)[-\s]*13\b", line2, re.I)
            or re.search(r"(?:গ্রেড|grade)[-\s]*14\b", line2, re.I)
            or "গ্রেড-১৩ এবং গ্রেড-১৪" in line2
            or "grade-13 and grade-14" in norm
        ):
            out.append(line)
            continue

        if grade_no == "15" and re.search(r"(?:গ্রেড|grade)[-\s]*15\b", line2, re.I):
            out.append(line)
            continue

        if grade_no == "16" and re.search(r"(?:গ্রেড|grade)[-\s]*16\b", line2, re.I):
            out.append(line)
            continue

    deduped: List[str] = []
    seen: set[str] = set()
    for x in out:
        k = _normalize_for_match(x)
        if k and k not in seen:
            seen.add(k)
            deduped.append(x)

    return deduped


def _find_authority_lines_for_education(ctx: str) -> List[str]:
    lines = [ln.strip() for ln in ctx.splitlines() if ln.strip()]
    out: List[str] = []
    for line in lines:
        norm = _normalize_for_match(line)
        if any(
            x in norm
            for x in [
                "গভর্ণিং বডি",
                "মানব সম্পদ ব্যবস্থাপনা বিভাগ",
                "মানবসম্পদ ব্যবস্থাপনা বিভাগ",
                "governing body",
                "human resource management division",
                "hr management division",
                "বিধি প্রণয়ন",
            ]
        ):
            out.append(line)

    deduped: List[str] = []
    seen: set[str] = set()
    for x in out:
        k = _normalize_for_match(x)
        if k and k not in seen:
            seen.add(k)
            deduped.append(x)

    return deduped


def _find_driver_exception_lines(ctx: str) -> List[str]:
    lines = [ln.strip() for ln in ctx.splitlines() if ln.strip()]
    out: List[str] = []
    for line in lines:
        norm = _normalize_for_match(line)
        if any(
            x in norm
            for x in [
                "গাড়ী চালক",
                "গাড়ি চালক",
                "driver",
                "শিথিল",
                "relaxation",
                "শিথিল করা যাবে",
            ]
        ):
            out.append(line)

    deduped: List[str] = []
    seen: set[str] = set()
    for x in out:
        k = _normalize_for_match(x)
        if k and k not in seen:
            seen.add(k)
            deduped.append(x)

    return deduped


def _format_simple_answer(summary: str, bullets: List[str], citations: List[Dict[str, Any]]) -> str:
    src = ", ".join([f"[{c['idx']}]" for c in citations[:5]]) if citations else "[1]"
    out = [f"Summary: {summary}", "Rules:"]
    for b in bullets:
        out.append(f"- {b}")
    out.append(f"Sources: {src}")
    return "\n".join(out)


# =========================================================
# Topic-specific deterministic handlers
# =========================================================
def _answer_grade_structure(question_raw: str, ctx: str, citations: List[Dict[str, Any]]) -> Optional[str]:
    intent = _detect_answer_intent(question_raw)
    grade_no = _extract_grade_number(question_raw)

    if intent == "count":
        return _format_simple_answer(
            "The policy defines 16 grades.",
            ["Total number of grades: 16."],
            citations,
        )

    if intent == "highest":
        line = _find_first_grade_line(ctx)
        if line:
            return _format_simple_answer(
                "Grade-1 is the highest grade.",
                [line],
                citations,
            )

    if intent == "lowest":
        line = _find_last_grade_line(ctx)
        if line:
            return _format_simple_answer(
                "Grade-16 is the lowest grade.",
                [line],
                citations,
            )

    if grade_no and _is_grade_lookup_question(question_raw):
        line = _find_grade_line(ctx, grade_no)
        if line:
            return _format_simple_answer(
                f"গ্রেড-{grade_no} এ নিম্নোক্ত পদ আছে।",
                [line],
                citations,
            )

    if intent in {"list", "definition", "explanation"}:
        lines = [ln.strip() for ln in ctx.splitlines() if ln.strip()]
        return _format_simple_answer(
            "The policy defines a 16-grade structure.",
            lines,
            citations,
        )

    return None


def _answer_educational_qualification(
    question_raw: str,
    ctx: str,
    citations: List[Dict[str, Any]],
) -> Optional[str]:
    intent = _detect_answer_intent(question_raw)
    qn = _normalize_for_match(question_raw)
    grade_no = _extract_grade_number(question_raw)
    all_grades = _extract_all_grade_numbers(question_raw)
    lines = [ln.strip() for ln in ctx.splitlines() if ln.strip()]

    if not lines:
        return None

    is_authority = any(
        x in qn
        for x in [
            "কে নির্ধারণ",
            "কারা নির্ধারণ",
            "কর্তৃপক্ষ",
            "মানব সম্পদ",
            "মানবসম্পদ",
            "গভর্ণিং বডি",
            "who determines",
            "who decides",
            "authority",
            "governing body",
            "hr division",
            "human resource management division",
        ]
    )

    is_exception = any(
        x in qn
        for x in [
            "গাড়ি চালক",
            "গাড়ি চালক",
            "ড্রাইভার",
            "driver",
            "শিথিল",
            "relaxation",
            "ছাড়",
        ]
    )

    is_compare = intent == "comparison" or len(all_grades) >= 2
    is_range = any(
        x in qn
        for x in [
            "থেকে",
            "পর্যন্ত",
            "2 to 11",
            "2-11",
            "১১ পর্যন্ত",
            "গ্রেড-২ থেকে গ্রেড-১১",
            "grades 2 to 11",
        ]
    )

    if is_authority:
        auth_lines = _find_authority_lines_for_education(ctx)
        if auth_lines:
            return _format_simple_answer(
                "নীতিমালায় শিক্ষাগত যোগ্যতা নির্ধারণের কর্তৃপক্ষ ও দায়িত্ব এভাবে বলা হয়েছে।",
                auth_lines[:10],
                citations,
            )

    if is_exception:
        exc_lines = _find_driver_exception_lines(ctx)
        if exc_lines:
            return _format_simple_answer(
                "নীতিমালায় ড্রাইভার/গাড়ি চালকের ক্ষেত্রে ব্যতিক্রম এভাবে বলা হয়েছে।",
                exc_lines[:8],
                citations,
            )

    if grade_no and (_is_education_question(question_raw) or intent in {"definition", "eligibility", "explanation", "yes_no"}):
        specific = _find_grade_related_lines(ctx, grade_no)
        if specific:
            return _format_simple_answer(
                f"গ্রেড-{grade_no} এর শিক্ষাগত যোগ্যতা নীতিমালায় এভাবে বলা হয়েছে।",
                specific[:8],
                citations,
            )

    if is_compare and all_grades:
        compare_lines: List[str] = []
        for g in all_grades[:4]:
            compare_lines.extend(_find_grade_related_lines(ctx, g))

        deduped: List[str] = []
        seen: set[str] = set()
        for x in compare_lines:
            k = _normalize_for_match(x)
            if k and k not in seen:
                seen.add(k)
                deduped.append(x)

        if deduped:
            grades_text = ", ".join([f"গ্রেড-{g}" for g in all_grades[:4]])
            return _format_simple_answer(
                f"{grades_text} এর শিক্ষাগত যোগ্যতার প্রাসঙ্গিক নীতিমালা নিচে দেওয়া হলো।",
                deduped[:12],
                citations,
            )

    if is_range:
        range_lines: List[str] = []
        for line in lines:
            norm = _normalize_for_match(line)
            if any(
                x in norm
                for x in [
                    "গ্রেড-২ থেকে গ্রেড-১১",
                    "গ্রেড ২ থেকে গ্রেড ১১",
                    "গ্রেড-১২",
                    "গ্রেড-১৩",
                    "গ্রেড-১৪",
                    "গ্রেড-১৫",
                    "গ্রেড-১৬",
                    "grade-2 to grade-11",
                    "grade 12",
                    "grade 13",
                    "grade 14",
                    "grade 15",
                    "grade 16",
                ]
            ):
                range_lines.append(line)

        if range_lines:
            return _format_simple_answer(
                "গ্রেডভিত্তিক শিক্ষাগত যোগ্যতা নীতিমালায় এভাবে নির্ধারণ করা হয়েছে।",
                range_lines[:12],
                citations,
            )

    if intent in {"definition", "eligibility", "explanation", "yes_no", "list"} or _is_education_question(question_raw):
        return _format_simple_answer(
            "নীতিমালায় কর্মীদের পদভিত্তিক শিক্ষাগত যোগ্যতা এভাবে নির্ধারণ করা হয়েছে।",
            lines[:12],
            citations,
        )

    return None


def _answer_leave_authority(question_raw: str, ctx: str, citations: List[Dict[str, Any]]) -> Optional[str]:
    intent = _detect_answer_intent(question_raw)
    if intent in {"authority", "definition", "explanation", "yes_no"}:
        lines = [ln.strip() for ln in ctx.splitlines() if ln.strip()]
        return _format_simple_answer(
            "The policy specifies the leave-approving authority as follows.",
            lines[:8],
            citations,
        )
    return None


def _answer_distance_allowance(question_raw: str, ctx: str, citations: List[Dict[str, Any]]) -> Optional[str]:
    intent = _detect_answer_intent(question_raw)

    if intent == "rate":
        lines = [ln.strip() for ln in ctx.splitlines() if ln.strip()]
        return _format_simple_answer(
            "The policy states the distance allowance rate as follows.",
            lines[:8],
            citations,
        )

    if intent in {"eligibility", "definition", "explanation", "yes_no"}:
        lines = [ln.strip() for ln in ctx.splitlines() if ln.strip()]
        return _format_simple_answer(
            "The policy explains distance allowance eligibility and conditions as follows.",
            lines[:8],
            citations,
        )

    return None


def _answer_daily_travel_allowance(question_raw: str, ctx: str, citations: List[Dict[str, Any]]) -> Optional[str]:
    intent = _detect_answer_intent(question_raw)

    if intent in {"rate", "eligibility", "definition", "explanation", "yes_no"}:
        lines = [ln.strip() for ln in ctx.splitlines() if ln.strip()]
        return _format_simple_answer(
            "The policy explains daily travel allowance as follows.",
            lines[:10],
            citations,
        )

    return None


def _answer_food_lodging_daily(question_raw: str, ctx: str, citations: List[Dict[str, Any]]) -> Optional[str]:
    intent = _detect_answer_intent(question_raw)

    if intent in {"rate", "eligibility", "definition", "explanation", "comparison", "yes_no"}:
        lines = [ln.strip() for ln in ctx.splitlines() if ln.strip()]
        return _format_simple_answer(
            "The policy explains food, lodging, and daily allowance as follows.",
            lines[:10],
            citations,
        )

    return None


def _answer_overtime_allowance(question_raw: str, ctx: str, citations: List[Dict[str, Any]]) -> Optional[str]:
    intent = _detect_answer_intent(question_raw)

    if intent in {"eligibility", "rate", "definition", "explanation", "yes_no"}:
        lines = [ln.strip() for ln in ctx.splitlines() if ln.strip()]
        return _format_simple_answer(
            "The policy explains overtime allowance as follows.",
            lines[:10],
            citations,
        )

    return None


def _answer_leave_general(question_raw: str, ctx: str, citations: List[Dict[str, Any]]) -> Optional[str]:
    intent = _detect_answer_intent(question_raw)

    if intent in {"definition", "explanation", "procedure", "exception", "yes_no"}:
        lines = [ln.strip() for ln in ctx.splitlines() if ln.strip()]
        return _format_simple_answer(
            "The policy explains the general leave rules as follows.",
            lines[:10],
            citations,
        )

    return None


def _answer_leave_type_generic(topic_bn: str, question_raw: str, ctx: str, citations: List[Dict[str, Any]]) -> Optional[str]:
    intent = _detect_answer_intent(question_raw)

    if intent in {"definition", "eligibility", "procedure", "exception", "yes_no", "explanation"}:
        lines = [ln.strip() for ln in ctx.splitlines() if ln.strip()]
        return _format_simple_answer(
            f"The policy explains {topic_bn} as follows.",
            lines[:10],
            citations,
        )

    return None


def _answer_promotion_eligibility(question_raw: str, ctx: str, citations: List[Dict[str, Any]]) -> Optional[str]:
    intent = _detect_answer_intent(question_raw)

    if intent in {"eligibility", "definition", "explanation", "yes_no"}:
        grade_no = _extract_grade_number(question_raw)
        lines = [ln.strip() for ln in ctx.splitlines() if ln.strip()]

        if grade_no:
            specific = [ln for ln in lines if grade_no in _bn_to_en_digits(ln)]
            if specific:
                return _format_simple_answer(
                    f"The policy explains promotion eligibility for Grade-{grade_no} as follows.",
                    specific[:8],
                    citations,
                )

        return _format_simple_answer(
            "The policy explains promotion eligibility as follows.",
            lines[:10],
            citations,
        )

    return None


def _answer_attendance_recording(question_raw: str, ctx: str, citations: List[Dict[str, Any]]) -> Optional[str]:
    intent = _detect_answer_intent(question_raw)
    if intent in {"definition", "procedure", "explanation"}:
        lines = [ln.strip() for ln in ctx.splitlines() if ln.strip()]
        return _format_simple_answer(
            "The policy explains attendance recording as follows.",
            lines[:10],
            citations,
        )
    return None


def _answer_late_attendance_absence(question_raw: str, ctx: str, citations: List[Dict[str, Any]]) -> Optional[str]:
    intent = _detect_answer_intent(question_raw)
    if intent in {"definition", "explanation", "yes_no", "exception"}:
        lines = [ln.strip() for ln in ctx.splitlines() if ln.strip()]
        return _format_simple_answer(
            "The policy explains late attendance and absence as follows.",
            lines[:10],
            citations,
        )
    return None


def _answer_resignation_rules(question_raw: str, ctx: str, citations: List[Dict[str, Any]]) -> Optional[str]:
    intent = _detect_answer_intent(question_raw)
    if intent in {"procedure", "definition", "explanation", "yes_no"}:
        lines = [ln.strip() for ln in ctx.splitlines() if ln.strip()]
        return _format_simple_answer(
            "The policy explains resignation rules as follows.",
            lines[:10],
            citations,
        )
    return None


def _answer_retirement_normal(question_raw: str, ctx: str, citations: List[Dict[str, Any]]) -> Optional[str]:
    intent = _detect_answer_intent(question_raw)
    if intent in {"definition", "count", "explanation", "yes_no"}:
        lines = [ln.strip() for ln in ctx.splitlines() if ln.strip()]
        return _format_simple_answer(
            "The policy explains normal retirement as follows.",
            lines[:10],
            citations,
        )
    return None


def _answer_sexual_harassment_penalty(question_raw: str, ctx: str, citations: List[Dict[str, Any]]) -> Optional[str]:
    intent = _detect_answer_intent(question_raw)
    if intent in {"list", "definition", "explanation", "comparison"}:
        lines = [ln.strip() for ln in ctx.splitlines() if ln.strip()]
        return _format_simple_answer(
            "The policy explains the sexual harassment penalty levels as follows.",
            lines[:12],
            citations,
        )
    return None


TOPIC_HANDLERS: Dict[str, HandlerFn] = {
    "grade_structure": _answer_grade_structure,
    "educational_qualification": _answer_educational_qualification,
    "leave_authority": _answer_leave_authority,
    "distance_allowance": _answer_distance_allowance,
    "daily_travel_allowance": _answer_daily_travel_allowance,
    "food_lodging_daily": _answer_food_lodging_daily,
    "overtime_allowance": _answer_overtime_allowance,
    "leave_general": _answer_leave_general,
    "earned_leave": lambda q, c, s: _answer_leave_type_generic("earned leave", q, c, s),
    "casual_leave": lambda q, c, s: _answer_leave_type_generic("casual leave", q, c, s),
    "maternity_leave": lambda q, c, s: _answer_leave_type_generic("maternity leave", q, c, s),
    "paternity_leave": lambda q, c, s: _answer_leave_type_generic("paternity leave", q, c, s),
    "festival_leave": lambda q, c, s: _answer_leave_type_generic("festival leave", q, c, s),
    "sick_leave": lambda q, c, s: _answer_leave_type_generic("sick leave", q, c, s),
    "higher_education_leave": lambda q, c, s: _answer_leave_type_generic("higher education leave", q, c, s),
    "promotion_eligibility": _answer_promotion_eligibility,
    "attendance_recording": _answer_attendance_recording,
    "late_attendance_absence": _answer_late_attendance_absence,
    "resignation_rules": _answer_resignation_rules,
    "retirement_normal": _answer_retirement_normal,
    "sexual_harassment_penalty": _answer_sexual_harassment_penalty,
}


def _deterministic_answer_for_topic(
    question_raw: str,
    detected_topic: Optional[Dict[str, str]],
    ctx: str,
    citations: List[Dict[str, Any]],
) -> Optional[str]:
    if not detected_topic or not ctx.strip():
        return None

    topic_key = _clean(detected_topic.get("manual_key"))
    handler = TOPIC_HANDLERS.get(topic_key)
    if not handler:
        return None

    try:
        return handler(question_raw, ctx, citations)
    except Exception:
        return None


# =========================================================
# Answer cleanup and LLM formatting
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

    bullets_clean: List[str] = []
    seen: set[str] = set()

    for ln in lines:
        low = ln.lower()

        if ln.startswith("Summary:") or ln == "Rules:" or low.startswith("sources:"):
            continue
        if "source_file=" in low or "topic_bn=" in low or "topic_en=" in low:
            continue

        clean_line = ln if ln.startswith("-") else "- " + ln
        nb = _normalize_for_match(clean_line)
        if len(nb) < 6 or nb in seen:
            continue

        seen.add(nb)
        bullets_clean.append(clean_line)

    out = [summary, "Rules:"]
    for b in bullets_clean[:30]:
        out.append(b)

    if not bullets_clean:
        out.append("- Policy does not state this clearly.")

    src = ", ".join([f"[{c['idx']}]" for c in citations[:5]]) if citations else "[1]"
    out.append(f"Sources: {src}")
    return "\n".join(out).strip()


def _build_messages(
    question_raw: str,
    ctx: str,
    mode: str = "summary",
    answer_intent: str = "explanation",
) -> List[Dict[str, str]]:
    system_prompt = SYSTEM_POLICY_FULL if mode == "full" else SYSTEM_POLICY_SUMMARY

    user_prompt = f"""Question: {question_raw}
User intent: {answer_intent}
Answer language: {"Bangla" if _is_bn(question_raw) else "English"}

Use ONLY the excerpts below.

Excerpts:
{ctx}
"""
    return [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}]


def _format_answer(
    question_raw: str,
    ctx: str,
    citations: List[Dict[str, Any]],
    detected_topic: Optional[Dict[str, Any]],
    mode: str = "summary",
) -> str:
    deterministic = _deterministic_answer_for_topic(question_raw, detected_topic, ctx, citations)
    if deterministic:
        return deterministic

    answer_intent = _detect_answer_intent(question_raw)
    raw = _ollama_chat(_build_messages(question_raw, ctx, mode=mode, answer_intent=answer_intent)).strip()

    cleaned = _clean_llm_answer(raw, citations)
    if cleaned and "Policy does not state this clearly" not in cleaned:
        return cleaned

    if ctx.strip():
        summary = (
            "নীতিমালার প্রাসঙ্গিক উদ্ধৃতি নিচে দেওয়া হলো।"
            if _is_bn(question_raw)
            else "The relevant policy excerpt is shown below."
        )
        lines = [ln.strip() for ln in ctx.splitlines() if ln.strip()]
        return _format_simple_answer(summary, lines, citations)

    return _format_simple_answer(
        "Policy does not state this clearly.",
        ["Policy does not state this clearly."],
        citations,
    )


# =========================================================
# Suggestions
# =========================================================
def _suggest_questions(topic: Optional[Dict[str, str]], question: str) -> List[str]:
    topic_id = _clean((topic or {}).get("topic_id"))
    topic_bn = _clean((topic or {}).get("topic_bn"))
    topic_en = _clean((topic or {}).get("topic_en"))

    if topic_id == "policy_1_3_1" or topic_bn == "গ্রেড কাঠামো":
        return [
            "মোট কয়টি গ্রেড আছে?",
            "গ্রেড-১ এ কোন পদ আছে?",
            "গ্রেড-১৬ এ কোন পদ আছে?",
        ]

    if topic_id == "policy_1_4" or topic_bn == "কর্মীদের পদ বিন্যাস অনুযায়ী শিক্ষাগত যোগ্যতা":
        return [
            "গ্রেড-২ থেকে গ্রেড-১১ পর্যন্ত ন্যূনতম শিক্ষাগত যোগ্যতা কী?",
            "গ্রেড-১২, গ্রেড-১৩ ও গ্রেড-১৪ এর শিক্ষাগত যোগ্যতার পার্থক্য কী?",
            "গ্রেড-১৬ গাড়ি চালকদের ক্ষেত্রে কি শিক্ষাগত যোগ্যতা শিথিল করা যায়?",
        ]

    if topic_en == "Daily Travel Allowance":
        return [
            "দৈনন্দিন যাতায়াত ভাতা কারা পাবে?",
            "এই ভাতা কত?",
            "কোন শর্তে এই ভাতা প্রযোজ্য?",
        ]

    if topic_en == "Food, Lodging and Daily Allowance":
        return [
            "খাদ্য ও অবস্থান ভাতা কত?",
            "মেস সুবিধা থাকলে কী নিয়ম হবে?",
            "স্থানভেদে ভাতা কীভাবে বদলায়?",
        ]

    if topic_en == "Overtime Allowance":
        return [
            "অধিকাল ভাতা কারা পাবে?",
            "ছুটির দিনে ওভারটাইমের নিয়ম কী?",
            "গ্রেড-১৬ কর্মীদের জন্য কী নিয়ম আছে?",
        ]

    if topic_en == "Leave Sanctioning Authority":
        return [
            "আমার ছুটি কে অনুমোদন করবে?",
            "কোন পদ কোন ধরনের ছুটি অনুমোদন করতে পারে?",
            "ছুটি অনুমোদনকারী কর্তৃপক্ষের নিয়ম কী?",
        ]

    q = _normalize_for_match(question)
    if "পদোন্নতি" in q or "promotion" in q:
        return [
            "পদোন্নতির যোগ্যতা কী?",
            "কত বছর পর পদোন্নতি পাওয়া যায়?",
            "গ্রেডভিত্তিক পদোন্নতির নিয়ম কী?",
        ]

    if "ছুটি" in q or "leave" in q:
        return [
            "এই ছুটি কারা পাবে?",
            "এই ছুটি কতদিন?",
            "এই ছুটি কে অনুমোদন করবে?",
        ]

    return [
        "এই বিষয়ে বিস্তারিত কী?",
        "এই নীতির মূল শর্তগুলো কী?",
        "এখানে কোনো ব্যতিক্রম আছে কি?",
    ]


# =========================================================
# Retrieval helpers
# =========================================================
def _score_pass(hit: dict) -> bool:
    try:
        return float(hit.get("score") or 0.0) >= float(CFG.SCORE_THRESHOLD)
    except Exception:
        return True


def _payload_text(hit: dict) -> str:
    payload = hit.get("payload") or {}
    return _clean(payload.get("text") or payload.get("content") or payload.get("chunk") or payload.get("prompt"))


def _topic_text_matches(hit: dict, detected_topic: Optional[Dict[str, str]]) -> bool:
    if not detected_topic:
        return True

    topic_key = _clean(detected_topic.get("manual_key"))
    if not topic_key:
        return True

    checks = TOPIC_TEXT_CHECKS.get(topic_key) or []
    if not checks:
        return True

    payload = hit.get("payload") or {}
    blob = " ".join(
        [
            _clean(payload.get("topic_bn")),
            _clean(payload.get("topic_en")),
            _clean(payload.get("clause_range")),
            _clean(payload.get("text")),
        ]
    )
    norm = _normalize_for_match(blob)
    return any(_normalize_for_match(c) in norm for c in checks)


def _filter_hits(hits: List[dict], detected_topic: Optional[Dict[str, str]]) -> List[dict]:
    out: List[dict] = []
    for h in hits:
        if not _score_pass(h):
            continue
        if not _payload_text(h):
            continue
        if not _topic_text_matches(h, detected_topic):
            continue
        out.append(h)
    return out


def _retrieve_hits(
    question_raw: str,
    rewritten_human_query: str,
    detected_topic: Optional[Dict[str, str]],
    collection: str,
    qfilter: Optional[dict],
    top_k: int,
) -> Tuple[str, List[dict]]:
    q_for_embed = _rewrite_query(rewritten_human_query, detected_topic)
    try:
        vec = _ollama_embed(q_for_embed)
    except Exception:
        vec = _ollama_embed(question_raw)

    hits = _qdrant_search(collection, vec, top_k=top_k, qfilter=qfilter)
    hits = _filter_hits(hits, detected_topic)

    if not hits and qfilter:
        hits = _qdrant_search(collection, vec, top_k=top_k, qfilter=None)
        hits = _filter_hits(hits, detected_topic)

    if not hits and q_for_embed != question_raw:
        vec2 = _ollama_embed(question_raw)
        hits = _qdrant_search(collection, vec2, top_k=top_k, qfilter=qfilter)
        hits = _filter_hits(hits, detected_topic)
        q_for_embed = question_raw

    return q_for_embed, hits


# =========================================================
# Main response builder
# =========================================================
def _run_policy_query(
    request,
    *,
    question_raw: str,
    collection: str,
    top_k: int,
    mode: str,
    stream_clean_final: int,
) -> StreamingHttpResponse:
    followup = _is_followup_request(question_raw)
    last_topic = _session_get_last_topic(request)

    rewritten_human_query = _rewrite_user_question_humanized(question_raw)

    detected_topic: Optional[Dict[str, str]]
    if followup and last_topic:
        detected_topic = dict(last_topic)
    else:
        detected_topic = _detect_best_topic(rewritten_human_query, collection)

    if detected_topic:
        _session_set_last_topic(request, detected_topic)
    else:
        _session_clear_last_topic(request)

    qfilter = _build_exact_topic_filter(detected_topic)
    q_for_embed, hits = _retrieve_hits(
        question_raw=question_raw,
        rewritten_human_query=rewritten_human_query,
        detected_topic=detected_topic,
        collection=collection,
        qfilter=qfilter,
        top_k=top_k,
    )

    rag = _build_rag_context(hits, mode=mode)
    ctx = rag["context"]
    citations = rag["citations"]

    if not ctx.strip():
        msg = (
            "প্রাসঙ্গিক নীতিমালার তথ্য স্পষ্টভাবে পাওয়া যায়নি।"
            if _is_bn(question_raw)
            else "A clear policy excerpt was not found."
        )
        return StreamingHttpResponse(
            [_ndjson({
                "type": "final",
                "answer": msg,
                "topic_detected": detected_topic,
                "suggested_questions": _suggest_questions(detected_topic, question_raw),
                "citations": [],
                "debug": {
                    "detected_topic": detected_topic,
                    "filter_used": qfilter,
                    "rewritten_human_query": rewritten_human_query,
                    "rewritten_query": q_for_embed,
                    "reason": "empty_context",
                    "mode": mode,
                    "followup_used_last_topic": bool(followup and last_topic),
                },
            })],
            content_type="application/x-ndjson; charset=utf-8",
        )

    if stream_clean_final == 1:
        answer = _format_answer(question_raw, ctx, citations, detected_topic, mode=mode)
        return StreamingHttpResponse(
            [_ndjson({
                "type": "final",
                "answer": answer,
                "topic_detected": detected_topic,
                "suggested_questions": _suggest_questions(detected_topic, question_raw),
                "citations": citations,
                "debug": {
                    "detected_topic": detected_topic,
                    "filter_used": qfilter,
                    "rewritten_human_query": rewritten_human_query,
                    "rewritten_query": q_for_embed,
                    "hits_count": len(hits),
                    "context_chars": len(ctx),
                    "chat_model": CFG.CHAT_MODEL,
                    "mode": mode,
                    "answer_intent": _detect_answer_intent(question_raw),
                    "followup_used_last_topic": bool(followup and last_topic),
                },
            })],
            content_type="application/x-ndjson; charset=utf-8",
        )

    def event_stream() -> Iterable[str]:
        yield _ndjson({
            "type": "meta",
            "topic_detected": detected_topic,
            "citations": citations,
            "suggested_questions": _suggest_questions(detected_topic, question_raw),
            "debug": {
                "detected_topic": detected_topic,
                "filter_used": qfilter,
                "rewritten_human_query": rewritten_human_query,
                "rewritten_query": q_for_embed,
                "hits_count": len(hits),
                "context_chars": len(ctx),
                "chat_model": CFG.CHAT_MODEL,
                "mode": mode,
                "answer_intent": _detect_answer_intent(question_raw),
                "followup_used_last_topic": bool(followup and last_topic),
            },
        })

        final_parts: List[str] = []
        answer_intent = _detect_answer_intent(question_raw)

        for item in _ollama_chat_stream(_build_messages(question_raw, ctx, mode=mode, answer_intent=answer_intent)):
            yield item
            try:
                obj = json.loads(item)
                if obj.get("type") == "token":
                    final_parts.append(obj.get("content") or "")
            except Exception:
                pass

        final_text = "".join(final_parts).strip()
        cleaned = _clean_llm_answer(final_text, citations)
        if cleaned:
            yield _ndjson({
                "type": "final_clean",
                "answer": cleaned,
                "topic_detected": detected_topic,
                "suggested_questions": _suggest_questions(detected_topic, question_raw),
                "citations": citations,
            })

    return StreamingHttpResponse(event_stream(), content_type="application/x-ndjson; charset=utf-8")


# =========================================================
# Views
# =========================================================
@csrf_exempt
@require_POST
def policy_ask(request):
    body = _json_body(request)

    question_raw = _clean(body.get("question") or body.get("query") or body.get("message"))
    if not question_raw:
        return JsonResponse({"ok": False, "error": "Missing question"}, status=400)

    collection = _clean(body.get("collection")) or CFG.POLICY_COLLECTION
    top_k = _safe_int(body.get("top_k"), CFG.TOP_K)
    mode = _clean(body.get("mode") or "summary").lower()
    if mode not in {"summary", "full"}:
        mode = "summary"

    use_chat_answer = _safe_int(body.get("use_chat_answer"), CFG.USE_CHAT_ANSWER)
    stream = _safe_int(body.get("stream"), 0)
    stream_clean_final = _safe_int(body.get("stream_clean_final"), 1)

    if stream == 1:
        return _run_policy_query(
            request,
            question_raw=question_raw,
            collection=collection,
            top_k=top_k,
            mode=mode,
            stream_clean_final=stream_clean_final,
        )

    followup = _is_followup_request(question_raw)
    last_topic = _session_get_last_topic(request)

    rewritten_human_query = _rewrite_user_question_humanized(question_raw)
    if followup and last_topic:
        detected_topic = dict(last_topic)
    else:
        detected_topic = _detect_best_topic(rewritten_human_query, collection)

    if detected_topic:
        _session_set_last_topic(request, detected_topic)
    else:
        _session_clear_last_topic(request)

    qfilter = _build_exact_topic_filter(detected_topic)
    q_for_embed, hits = _retrieve_hits(
        question_raw=question_raw,
        rewritten_human_query=rewritten_human_query,
        detected_topic=detected_topic,
        collection=collection,
        qfilter=qfilter,
        top_k=top_k,
    )

    rag = _build_rag_context(hits, mode=mode)
    ctx = rag["context"]
    citations = rag["citations"]

    if not ctx.strip():
        msg = (
            "প্রাসঙ্গিক নীতিমালার তথ্য স্পষ্টভাবে পাওয়া যায়নি।"
            if _is_bn(question_raw)
            else "A clear policy excerpt was not found."
        )
        return JsonResponse(
            {
                "ok": True,
                "answer": msg,
                "topic_detected": detected_topic,
                "suggested_questions": _suggest_questions(detected_topic, question_raw),
                "citations": [],
                "debug": {
                    "detected_topic": detected_topic,
                    "filter_used": qfilter,
                    "rewritten_human_query": rewritten_human_query,
                    "rewritten_query": q_for_embed,
                    "reason": "empty_context",
                    "mode": mode,
                    "followup_used_last_topic": bool(followup and last_topic),
                },
            },
            json_dumps_params={"ensure_ascii": False},
        )

    if use_chat_answer == 1:
        answer = _format_answer(question_raw, ctx, citations, detected_topic, mode=mode)
    else:
        answer = _format_simple_answer(
            "নীতিমালার প্রাসঙ্গিক উদ্ধৃতি নিচে দেওয়া হলো。" if _is_bn(question_raw) else "The relevant policy excerpt is shown below.",
            [ln.strip() for ln in ctx.splitlines() if ln.strip()],
            citations,
        )

    return JsonResponse(
        {
            "ok": True,
            "answer": answer,
            "topic_detected": detected_topic,
            "suggested_questions": _suggest_questions(detected_topic, question_raw),
            "citations": citations,
            "debug": {
                "detected_topic": detected_topic,
                "filter_used": qfilter,
                "rewritten_human_query": rewritten_human_query,
                "rewritten_query": q_for_embed,
                "hits_count": len(hits),
                "context_chars": len(ctx),
                "chat_model": CFG.CHAT_MODEL,
                "mode": mode,
                "answer_intent": _detect_answer_intent(question_raw),
                "followup_used_last_topic": bool(followup and last_topic),
            },
        },
        json_dumps_params={"ensure_ascii": False},
    )


@csrf_exempt
@require_POST
def policy_ask_stream(request):
    body = _json_body(request)
    body["stream"] = 1
    request._body = json.dumps(body, ensure_ascii=False).encode("utf-8")
    return policy_ask(request)
