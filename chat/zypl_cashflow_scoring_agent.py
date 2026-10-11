from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

# Load Zypl config from local file so Gunicorn/systemd export is not required.
def _load_zypl_env_file():
    env_path = "/home/mamun/finalapp/app/.zypl.env"
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except FileNotFoundError:
        pass

_load_zypl_env_file()


SESSION_KEY = "zypl_cashflow_score_agent_state"
CLIENT_APP_ID_SLOT = "__loan_application_id__"

ZYPL_BASE_URL = os.environ.get(
    "ZYPL_BASE_URL",
    "https://zypl-dynamic.westeurope.cloudapp.azure.com/api",
).rstrip("/")

ZYPL_USERNAME = os.environ.get("ZYPL_USERNAME", "mamun.it@burobd.og")
ZYPL_PASSWORD = os.environ.get("ZYPL_PASSWORD", "123456")
ZYPL_PARTNER_ID = os.environ.get("ZYPL_PARTNER_ID", "35")
ZYPL_MODEL_ID = os.environ.get("ZYPL_MODEL_ID", "caab7241-98fb-46dc-b4d7-aace23e2110c")
ZYPL_TIMEOUT_SECONDS = int(os.environ.get("ZYPL_TIMEOUT_SECONDS", "45"))

# Keep true in production.
ZYPL_REQUIRE_LOGIN = os.environ.get("ZYPL_REQUIRE_LOGIN", "true").strip().lower() in [
    "1",
    "true",
    "yes",
    "y",
    "on",
]

# This service is protected for cashflow role/group.
# You may override:
# export ZYPL_ALLOWED_ROLES='cashflow,admin'
ZYPL_ALLOWED_ROLES = [
    x.strip().lower()
    for x in os.environ.get("ZYPL_ALLOWED_ROLES", "cashflow").split(",")
    if x.strip()
]

TOKEN_CACHE: Dict[str, Any] = {
    "access_token": None,
    "created_at": 0,
}

ZYPL_INTENT_KEYWORDS = [
    "zypl",
    "e-step",
    "estep",
    "score client",
    "client score",
    "loan score",
    "loan risk",
    "risk score",
    "credit risk",
    "credit scoring",
    "cashflow score",
    "score this client",
    "এই ক্লায়েন্টের স্কোর",
    "ক্লায়েন্ট স্কোর",
    "ঋণ স্কোর",
    "ঝুঁকি স্কোর",
    "ক্রেডিট স্কোর",
    "স্কোর করো",
]

# User MUST provide these Step 1 fields.
USER_REQUIRED_REQUEST_FIELDS: List[str] = [
    "total_wealth",
    "monthly_payment",
    "interest_rate",
    "loan_amount",
    "duration_at_disbursement_months",
    "age",
    "quantity_of_prior_loans",
    "quantity_of_dependants",
    "branch",
    "location",
    "product_name",
    "type_of_product",
    "gender",
]

# These are added automatically by system to follow the full API guideline.
SYSTEM_DEFAULT_EXTRA_FIELDS: Dict[str, Any] = {
    "education_level": "Pre-Primary",
    "home_type": "TN",
    "remittances_value": 3755.05,
    "consumer_price_index_cpi_roll_2m": 143.92,
    "food_inflation_value": 8.24,
    "disaster_type": "Absent",
    "disaster_total_affected": 0,
    "disaster_cpi": 0,
}


# Valid default values for Step 1 fields.
# User can type: skip / default / . / empty Enter to use these.
USER_DEFAULT_STEP1_FIELDS: Dict[str, Any] = {
    "total_wealth": 10000,
    "monthly_payment": 1000,
    "interest_rate": 12,
    "loan_amount": 10000,
    "duration_at_disbursement_months": 12,
    "age": 34,
    "quantity_of_prior_loans": 3,
    "quantity_of_dependants": 5,
    "branch": "branch_11",
    "location": "district_other",
    "product_name": "Agriculture Loan",
    "type_of_product": "product_1.01",
    "gender": "Female",
}

SKIP_VALUE_WORDS = {
    "",
    "skip",
    "default",
    ".",
    "-",
    "n/a",
    "na",
    "use default",
    "ডিফল্ট",
    "স্কিপ",
    "বাদ",
}

FIELD_META: Dict[str, Dict[str, Any]] = {
    "total_wealth": {
        "type": "number",
        "example": 10000,
        "question_bn": "গ্রাহকের total_wealth কত?",
        "question_en": "What is the client's total_wealth?",
        "aliases": ["total_wealth", "total wealth", "wealth", "asset", "assets", "মোট সম্পদ"],
    },
    "monthly_payment": {
        "type": "number",
        "example": 1000,
        "question_bn": "monthly_payment কত?",
        "question_en": "What is the monthly_payment?",
        "aliases": ["monthly_payment", "monthly payment", "installment", "emi", "মাসিক কিস্তি"],
    },
    "interest_rate": {
        "type": "number",
        "example": 12,
        "question_bn": "interest_rate কত?",
        "question_en": "What is the interest_rate?",
        "aliases": ["interest_rate", "interest rate", "rate", "সুদের হার"],
    },
    "loan_amount": {
        "type": "number",
        "example": 10000,
        "question_bn": "loan_amount কত?",
        "question_en": "What is the loan_amount?",
        "aliases": ["loan_amount", "loan amount", "amount", "loan", "ঋণের পরিমাণ"],
    },
    "duration_at_disbursement_months": {
        "type": "integer",
        "example": 12,
        "question_bn": "duration_at_disbursement_months কত?",
        "question_en": "What is the duration_at_disbursement_months?",
        "aliases": ["duration_at_disbursement_months", "duration", "tenure", "months", "মেয়াদ"],
    },
    "age": {
        "type": "integer",
        "example": 34,
        "question_bn": "গ্রাহকের age কত?",
        "question_en": "What is the client's age?",
        "aliases": ["age", "client age", "বয়স"],
    },
    "quantity_of_prior_loans": {
        "type": "integer",
        "example": 3,
        "question_bn": "quantity_of_prior_loans কত?",
        "question_en": "How many prior loans?",
        "aliases": ["quantity_of_prior_loans", "prior loans", "previous loans", "আগের ঋণ"],
    },
    "quantity_of_dependants": {
        "type": "integer",
        "example": 5,
        "question_bn": "quantity_of_dependants কত?",
        "question_en": "How many dependants?",
        "aliases": ["quantity_of_dependants", "dependants", "dependents", "নির্ভরশীল"],
    },
    "branch": {
        "type": "string",
        "example": "branch_other",
        "question_bn": "branch ভ্যালু কী?",
        "question_en": "What is the branch value?",
        "aliases": ["branch", "branch code", "ব্রাঞ্চ"],
    },
    "location": {
        "type": "string",
        "example": "district_other",
        "question_bn": "location ভ্যালু কী?",
        "question_en": "What is the location value?",
        "aliases": ["location", "district", "area", "লোকেশন", "জেলা"],
    },
    "product_name": {
        "type": "string",
        "example": "Agriculture Loan",
        "question_bn": "product_name কী?",
        "question_en": "What is the product_name?",
        "aliases": ["product_name", "product name", "product", "প্রোডাক্ট"],
    },
    "type_of_product": {
        "type": "string",
        "example": "product_1.01",
        "question_bn": "type_of_product কী?",
        "question_en": "What is the type_of_product?",
        "aliases": ["type_of_product", "type of product", "product type", "প্রোডাক্ট টাইপ"],
    },
    "gender": {
        "type": "string",
        "example": "Female",
        "question_bn": "gender কী?",
        "question_en": "What is the gender?",
        "aliases": ["gender", "sex", "লিঙ্গ"],
    },
}


class ZyplAPIError(Exception):
    pass





ZYPL_HELP_KEYWORDS = [
    "speak about zypl",
    "speak about zypl score",
    "tell me about zypl",
    "tell about zypl",
    "zypl help",
    "zypl service",
    "zypl services",
    "zypl capability",
    "zypl capabilities",
    "zypl question",
    "zypl questions",
    "zypl sample question",
    "zypl sample questions",
    "list zypl",
    "list zypl questions",
    "how to use zypl",
    "what can zypl do",
    "what is zypl score",
    "জাইপল সম্পর্কে বল",
    "zypl সম্পর্কে বল",
    "zypl প্রশ্ন",
    "zypl কী করতে পারে",
]


def is_zypl_help_intent(message: str) -> bool:
    msg = (message or "").strip().lower()

    if not msg:
        return False

    if any(k in msg for k in ZYPL_HELP_KEYWORDS):
        return True

    if ("zypl" in msg or "ypl" in msg or "zyp" in msg) and any(
        w in msg
        for w in [
            "help",
            "service",
            "services",
            "capability",
            "capabilities",
            "question",
            "questions",
            "sample",
            "about",
            "speak",
            "list",
            "how to use",
        ]
    ):
        return True

    return False


def build_zypl_help_text() -> str:
    return """Zypl Cashflow Scoring Agent — Available Services

1. New Client Scoring
   User can submit required Step 1 fields and the system will call Zypl live scoring API.

2. Skip / Default Scoring Test
   User can type skip or skip all during scoring. The system will use valid default values for quick testing.

3. Fetch Scoring Logs / Results
   User can fetch previous Zypl scoring records from Zypl scoring_logs API.

4. Search by Date Range
   User can ask for all Zypl records between two dates.

5. Search by Application ID
   User can search result by client_app_id / Loan Application ID.

6. Search by Scoring ID / Record ID
   User can search a specific Zypl scoring transaction.

7. Search by Customer ID
   User can search result using customer_id inside Zypl value_filters.

8. Recent Records
   User can ask last/top/recent Zypl records.

9. Reset Session
   User can reset scoring or logs conversation session.

Sample Questions for New Zypl Score

- zypl score client
- I want to score a client using zypl
- cashflow score client
- e-step score client
- credit risk score for a client
- loan risk score using zypl
- ক্লায়েন্টের Zypl score করো

During scoring:
- skip
- default
- .
- skip all
- reset

Sample Questions for Zypl Logs / Results

- show all zypl records between 2026-05-01 and 2026-05-31
- give all zypl records between 2026-05-01 and 2026-05-31
- show zypl scoring logs from 2026-05-01 to 2026-05-31
- zypl logs last 20
- show recent zypl results
- fetch top 50 zypl scoring logs
- zypl logs client_app_id APP-20260517-21789916
- show zypl result for APP-20260517-21789916
- zypl result scoring_id 4551
- show zypl record id 4551
- zypl result customer_id 1721789916
- show zypl logs for customer id 1721789916
 upload excel for zypl scoring
- process zypl batch file
- generate zypl batch score result
- show zypl batch upload format
- what columns are required for zypl batch scoring?
- জাইপল ব্যাচ স্কোর করো
- এক্সেল ফাইল দিয়ে Zypl score করো
- একসাথে অনেক ক্লায়েন্টের Zypl score করো

Access Control

This feature is protected for cashflow role/group users.
"""


def process_zypl_help_agent(request, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "ok": True,
        "intent": "zypl_cashflow_help",
        "tool": "zypl_cashflow_help",
        "status": "help",
        "answer": build_zypl_help_text(),
        "required_role": "cashflow",
        "public": False,
        "login_required": True,
    }


def is_zypl_intent(message: str) -> bool:
    """
    Detect only Zypl scoring/create-score intent.
    Logs/result/help queries must not start scoring.
    """
    msg = (message or "").strip().lower()

    if not msg:
        return False

    # Help/about/list queries should not start scoring.
    try:
        if is_zypl_help_intent(msg):
            return False
    except NameError:
        pass

    # Logs/result/history queries should not start scoring.
    log_words = [
        "log",
        "logs",
        "record",
        "records",
        "result",
        "results",
        "history",
        "previous",
        "show all",
        "give all",
        "scoring_id",
        "record_id",
        "customer_id",
        "রেকর্ড",
        "রেজাল্ট",
        "লগ",
        "ইতিহাস",
    ]

    if ("zypl" in msg or "ypl" in msg or "zyp" in msg) and any(w in msg for w in log_words):
        return False

    scoring_keywords = [
        "zypl score",
        "ypl score",
        "zyp score",
        "e-step score",
        "estep score",
        "cashflow score",
        "cashflow scoring",
        "credit scoring",
        "credit risk",
        "risk score",
        "loan risk",
        "loan score",
        "score client",
        "client score",
        "score this client",
        "scoring client",
        "new score",
        "create score",

        # Bengali scoring intents
        "ক্লায়েন্ট স্কোর",
        "ক্লায়েন্ট স্কোর",
        "ক্লায়েন্টের স্কোর",
        "ক্লায়েন্টের স্কোর",
        "ক্লায়েন্টের zypl score",
        "ঋণ স্কোর",
        "ঋণের স্কোর",
        "ঋণ ঝুঁকি স্কোর",
        "ঋণের ঝুঁকি স্কোর",
        "ঋণের ঝুঁকি স্কোর করো",
        "ঋণের ঝুঁকি স্কোর করুন",
        "লোন ঝুঁকি স্কোর",
        "লোন স্কোর",
        "ঝুঁকি স্কোর",
        "ঝুঁকি স্কোর করো",
        "ক্রেডিট স্কোর",
        "ক্রেডিট রিস্ক স্কোর",
        "স্কোর করো",
        "স্কোর করুন",
        "জাইপল স্কোর",
        "zypl স্কোর",
    ]

    if any(k in msg for k in scoring_keywords):
        return True

    # Flexible Bengali pattern: loan/risk + score
    if any(w in msg for w in ["ঋণ", "ঋণের", "লোন", "ঝুঁকি", "ক্রেডিট"]) and any(
        w in msg for w in ["স্কোর", "score", "scoring"]
    ):
        return True

    return False

def build_client_app_id() -> str:
    return "APP-" + time.strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:8].upper()


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "****"
    return value[:2] + "****" + value[-2:]


def _get_user_role_values(request) -> List[str]:
    user = getattr(request, "user", None)
    values: List[str] = []

    if not user:
        return values

    for attr in ["role", "user_role", "module", "department"]:
        value = getattr(user, attr, None)
        if value:
            values.append(str(value).lower())

    for profile_attr in ["profile", "userprofile"]:
        profile = getattr(user, profile_attr, None)
        if profile:
            for attr in ["role", "user_role", "module", "department"]:
                value = getattr(profile, attr, None)
                if value:
                    values.append(str(value).lower())

    try:
        values.extend([x.lower() for x in user.groups.values_list("name", flat=True)])
    except Exception:
        pass

    return values


def user_can_use_zypl(request) -> bool:
    if not ZYPL_REQUIRE_LOGIN:
        return True

    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return False

    if getattr(user, "is_superuser", False):
        return True

    role_values = set(_get_user_role_values(request))
    allowed = set(ZYPL_ALLOWED_ROLES)

    return bool(role_values.intersection(allowed))


def to_ascii_digits(value: Any) -> str:
    bangla_digits = "০১২৩৪৫৬৭৮৯"
    english_digits = "0123456789"
    return str(value).translate(str.maketrans(bangla_digits, english_digits))


def normalize_value(field: str, value: Any) -> Any:
    meta = FIELD_META.get(field, {})
    field_type = meta.get("type", "string")

    if value is None:
        return None

    if field_type == "string":
        text = str(value).strip()

        # Zypl category normalization.
        # User may enter _104 or 104, but Zypl expects district_104.
        if field == "location":
            if text.startswith("_") and text[1:].isdigit():
                return "district" + text
            if text.isdigit():
                return "district_" + text
            if text.lower() in ["other", "district other", "district_other", "default", "skip", "."]:
                return "district_other"
            return text

        # User may enter _107 or 107, but Zypl expects branch_107.
        if field == "branch":
            if text.startswith("_") and text[1:].isdigit():
                return "branch" + text
            if text.isdigit():
                return "branch_" + text
            if text.lower() in ["other", "branch other", "branch_other", "default", "skip", "."]:
                return "branch_other"
            return text

        # User may enter 1.01 or _1.01, but Zypl expects product_1.01.
        if field == "type_of_product":
            low = text.lower()
            if low in ["default", "skip", "."]:
                return "product_1.01"
            if text.startswith("_"):
                return "product" + text
            if re.fullmatch(r"\d+(\.\d+)?", text):
                return "product_" + text
            return text

        if field == "product_name":
            if text.lower() in ["default", "skip", "."]:
                return "Agriculture Loan"
            return text

        if field == "gender":
            low = text.lower()
            if low in ["f", "female", "woman", "মহিলা", "নারী", "default", "skip", "."]:
                return "Female"
            if low in ["m", "male", "man", "পুরুষ"]:
                return "Male"
            return text

        return text

    if isinstance(value, (int, float)):
        return int(value) if field_type == "integer" else float(value)

    text = to_ascii_digits(value).strip().replace(",", "")
    match = re.search(r"-?\d+(\.\d+)?", text)

    if not match:
        return value

    number_text = match.group(0)

    if field_type == "integer":
        return int(float(number_text))

    return float(number_text)

def get_agent_state(request) -> Dict[str, Any]:
    state = request.session.get(SESSION_KEY)

    if not isinstance(state, dict):
        state = {}

    state.setdefault("client_app_id", "")
    state.setdefault("generated_client_app_id", build_client_app_id())
    state.setdefault("group_scoring", False)
    state.setdefault("request_data_user_inputs", {})
    state.setdefault("waiting_for_field", None)
    state.setdefault("started_at", int(time.time()))

    return state


def save_agent_state(request, state: Dict[str, Any]) -> None:
    request.session[SESSION_KEY] = state
    request.session.modified = True


def clear_agent_state(request) -> None:
    if SESSION_KEY in request.session:
        del request.session[SESSION_KEY]
        request.session.modified = True


def is_reset_message(message: str) -> bool:
    return (message or "").strip().lower() in [
        "reset",
        "clear",
        "cancel",
        "restart",
        "রিসেট",
        "বাতিল",
    ]


def parse_client_app_id_from_payload(payload: Dict[str, Any]) -> str:
    for key in ["client_app_id", "loan_application_id", "Loan Application ID", "application_id"]:
        value = payload.get(key)
        if value:
            return str(value).strip()
    return ""


def parse_client_app_id_from_message(message: str) -> str:
    if not message:
        return ""

    text = str(message).strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parse_client_app_id_from_payload(parsed)
    except Exception:
        pass

    patterns = [
        r"loan\s*application\s*id\s*(?:=|:|-|is)?\s*([A-Za-z0-9._/-]+)",
        r"client\s*app\s*id\s*(?:=|:|-|is)?\s*([A-Za-z0-9._/-]+)",
        r"application\s*id\s*(?:=|:|-|is)?\s*([A-Za-z0-9._/-]+)",
        r"\b(APP-[A-Za-z0-9._/-]+)\b",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()

    return ""


def parse_request_data_from_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    source = None

    if isinstance(payload.get("request_data"), dict):
        source = payload["request_data"]
    elif isinstance(payload.get("data"), dict):
        source = payload["data"]

    if not isinstance(source, dict):
        return {}

    parsed: Dict[str, Any] = {}

    for key, value in source.items():
        key = str(key).strip()
        if key in USER_REQUIRED_REQUEST_FIELDS:
            parsed[key] = normalize_value(key, value)

    return parsed


def parse_request_data_from_message(message: str) -> Dict[str, Any]:
    parsed_fields: Dict[str, Any] = {}

    if not message:
        return parsed_fields

    text = to_ascii_digits(message).strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            source = parsed.get("request_data") if isinstance(parsed.get("request_data"), dict) else parsed
            if isinstance(source, dict):
                for key, value in source.items():
                    key = str(key).strip()
                    if key in USER_REQUIRED_REQUEST_FIELDS:
                        parsed_fields[key] = normalize_value(key, value)
            return parsed_fields
    except Exception:
        pass

    for field in USER_REQUIRED_REQUEST_FIELDS:
        aliases = FIELD_META.get(field, {}).get("aliases", [field])

        for alias in aliases:
            pattern = re.compile(
                rf"(?:^|[\s,;]){re.escape(alias)}\s*(?:=|:|-|is|হলো|হল)?\s*([^,\n;]+)",
                flags=re.IGNORECASE,
            )
            match = pattern.search(text)
            if match:
                value = match.group(1).strip()
                if value:
                    parsed_fields[field] = normalize_value(field, value)
                    break

    return parsed_fields



def is_skip_value_message(message: str) -> bool:
    return (message or "").strip().lower() in SKIP_VALUE_WORDS


def get_default_step1_value(field: str) -> Any:
    if field not in USER_DEFAULT_STEP1_FIELDS:
        return None
    return normalize_value(field, USER_DEFAULT_STEP1_FIELDS[field])


def fill_missing_step1_defaults(user_inputs: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(user_inputs or {})
    for field in USER_REQUIRED_REQUEST_FIELDS:
        if merged.get(field) in [None, ""]:
            merged[field] = get_default_step1_value(field)
    return merged


def find_missing_user_fields(request_data_user_inputs: Dict[str, Any]) -> List[str]:
    missing = []

    for field in USER_REQUIRED_REQUEST_FIELDS:
        value = request_data_user_inputs.get(field)
        if value is None or value == "":
            missing.append(field)

    return missing


def build_question_for_field(field: str, collected: Dict[str, Any]) -> str:
    meta = FIELD_META[field]
    count = len(collected)
    total = len(USER_REQUIRED_REQUEST_FIELDS)

    default_value = USER_DEFAULT_STEP1_FIELDS.get(field, meta.get("example"))

    return (
        f"{meta['question_bn']}\n"
        f"{meta['question_en']}\n\n"
        f"Example: {meta['example']}\n"
        f"Default if skipped: {default_value}\n"
        f"Collected request_data fields: {count}/{total}\n\n"
        f"Type skip/default/. or press Enter to use default."
    )


def build_loan_application_id_question(state: Dict[str, Any]) -> str:
    generated = state.get("generated_client_app_id") or build_client_app_id()

    return (
        "Loan Application ID দিন।\n"
        "Please provide Loan Application ID.\n\n"
        f"Example: {generated}\n"
        "Type skip/default/. or press Enter to auto-generate this ID."
    )



def make_customer_id_int(client_app_id: str) -> int:
    """
    Zypl model requires customer_id as integer inside request_data.
    client_app_id can remain string, but customer_id must be numeric.
    Extract digits from Loan Application ID. If no digits exist, use current timestamp.
    """
    raw = str(client_app_id or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())

    if digits:
        # Keep it reasonably sized for API/model compatibility.
        return int(digits[-10:])

    return int(time.strftime("%Y%m%d%H"))


def build_full_request_data(user_inputs: Dict[str, Any], client_app_id: str = "") -> Dict[str, Any]:
    """
    Full Zypl API request_data = user Step 1 fields + system default extra fields.
    Zypl BURO model also requires customer_id inside request_data.
    We map customer_id from Loan Application ID / client_app_id.
    """
    request_data: Dict[str, Any] = {}

    # Dynamic model-required field
    request_data["customer_id"] = make_customer_id_int(client_app_id)

    for field in USER_REQUIRED_REQUEST_FIELDS:
        request_data[field] = normalize_value(field, user_inputs[field])

    for field, value in SYSTEM_DEFAULT_EXTRA_FIELDS.items():
        request_data[field] = value

    return request_data

def build_zypl_api_payload(
    client_app_id: str,
    request_data: Dict[str, Any],
    group_scoring: bool = False,
) -> list:
    return [
        {
            "client_app_id": client_app_id,
            "group_scoring": bool(group_scoring),
            "request_data": request_data,
        }
    ]


def zypl_login(force_refresh: bool = False) -> str:
    cached_token = TOKEN_CACHE.get("access_token")
    created_at = int(TOKEN_CACHE.get("created_at") or 0)

    if cached_token and not force_refresh and time.time() - created_at < 1500:
        return str(cached_token)

    login_url = f"{ZYPL_BASE_URL}/auth/login"

    try:
        response = requests.post(
            login_url,
            headers={"Content-Type": "application/json"},
            json={
                "login": ZYPL_USERNAME,
                "password": ZYPL_PASSWORD,
            },
            timeout=ZYPL_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise ZyplAPIError(f"Could not connect to Zypl login endpoint: {exc}") from exc

    if response.status_code >= 400:
        raise ZyplAPIError(
            f"Zypl login failed. HTTP {response.status_code}: {response.text[:1000]}"
        )

    try:
        data = response.json()
    except Exception as exc:
        raise ZyplAPIError(
            f"Zypl login returned non-JSON response: {response.text[:500]}"
        ) from exc

    access_token = data.get("access_token") or data.get("token") or data.get("token") or data.get("token")

    if not access_token:
        raise ZyplAPIError(
            "Zypl login did not return token/access_token. "
            "If OTP/2FA is enabled, Zypl team must provide OTP endpoint/flow."
        )

    TOKEN_CACHE["access_token"] = access_token
    TOKEN_CACHE["created_at"] = int(time.time())

    return str(access_token)


def zypl_score(
    client_app_id: str,
    request_data: Dict[str, Any],
    group_scoring: bool = False,
) -> Dict[str, Any]:
    token = zypl_login()

    score_url = f"{ZYPL_BASE_URL}/partner/{ZYPL_PARTNER_ID}/model/{ZYPL_MODEL_ID}/score"

    api_payload = build_zypl_api_payload(
        client_app_id=client_app_id,
        request_data=request_data,
        group_scoring=group_scoring,
    )

    try:
        response = requests.post(
            score_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=api_payload,
            timeout=ZYPL_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise ZyplAPIError(f"Could not connect to Zypl score endpoint: {exc}") from exc

    if response.status_code in [401, 403]:
        token = zypl_login(force_refresh=True)

        response = requests.post(
            score_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=api_payload,
            timeout=ZYPL_TIMEOUT_SECONDS,
        )

    if response.status_code >= 400:
        raise ZyplAPIError(
            f"Zypl scoring failed. HTTP {response.status_code}: {response.text[:1500]}"
        )

    try:
        return response.json()
    except Exception as exc:
        raise ZyplAPIError(
            f"Zypl score returned non-JSON response: {response.text[:500]}"
        ) from exc


def summarize_zypl_response(raw_response: Dict[str, Any]) -> Dict[str, Any]:
    scoring = raw_response.get("scoring") or []
    first = scoring[0] if scoring else {}

    return {
        "id": first.get("id"),
        "client_app_id": first.get("client_app_id"),
        "prediction": first.get("prediction"),
        "prediction_score": first.get("prediction_score"),
        "is_approved": first.get("is_approved"),
        "threshold": first.get("threshold"),
        "risk_status": first.get("scoring_risk_status"),
        "status": first.get("status"),
        "reason": first.get("reason"),
        "recommendation": first.get("recommendation"),
        "recommended": first.get("recommended"),
        "model_id": first.get("model_id"),
    }


def build_answer_text(summary: Dict[str, Any]) -> str:
    approved = summary.get("is_approved")

    if approved is True:
        decision = "অনুমোদিত / Approved"
    elif approved is False:
        decision = "অননুমোদিত / Not Approved"
    else:
        decision = "সিদ্ধান্ত পাওয়া যায়নি / Decision not available"

    lines = [
        "Zypl e-Step Scoring Result",
        "",
        f"Decision: {decision}",
        f"Risk Status: {summary.get('risk_status')}",
        f"Prediction: {summary.get('prediction')}",
        f"Threshold: {summary.get('threshold')}",
        f"Status: {summary.get('status')}",
        f"Loan Application ID: {summary.get('client_app_id')}",
        f"Transaction ID: {summary.get('id')}",
    ]

    if summary.get("reason"):
        lines.append(f"Reason: {summary.get('reason')}")

    if summary.get("recommendation"):
        lines.append(f"Recommendation: {summary.get('recommendation')}")

    return "\n".join(lines)


def process_zypl_agent(request, payload: Dict[str, Any]) -> Dict[str, Any]:
    message = str(
        payload.get("message")
        or payload.get("query")
        or payload.get("question")
        or ""
    ).strip()

    if not user_can_use_zypl(request):
        return {
            "ok": False,
            "intent": "zypl_cashflow_scoring",
            "tool": "zypl_cashflow_scoring_agent",
            "status": "permission_denied",
            "answer": "এই Zypl cashflow scoring service ব্যবহার করার জন্য cashflow role/group access প্রয়োজন।",
            "required_role": "cashflow",
            "public": False,
            "login_required": True,
        }

    if is_reset_message(message):
        clear_agent_state(request)
        return {
            "ok": True,
            "intent": "zypl_cashflow_scoring",
            "tool": "zypl_cashflow_scoring_agent",
            "status": "reset",
            "answer": "Zypl scoring session reset করা হয়েছে। আবার শুরু করতে লিখুন: zypl score client",
            "public": False,
            "login_required": True,
        }

    state = get_agent_state(request)

    # Shortcut: user can type "skip all" to fill all remaining Step 1 fields with defaults.
    if (message or "").strip().lower() in ["skip all", "default all", "সব ডিফল্ট", "সব স্কিপ"]:
        request_data_user_inputs = fill_missing_step1_defaults(state.get("request_data_user_inputs") or {})
        state["request_data_user_inputs"] = request_data_user_inputs
        if not state.get("client_app_id"):
            state["client_app_id"] = state.get("generated_client_app_id") or build_client_app_id()
        state["waiting_for_field"] = None
        save_agent_state(request, state)
        message = ""


    if "group_scoring" in payload:
        state["group_scoring"] = bool(payload.get("group_scoring"))

    client_app_id = parse_client_app_id_from_payload(payload)
    if not client_app_id:
        client_app_id = parse_client_app_id_from_message(message)

    if client_app_id:
        state["client_app_id"] = client_app_id

    request_data_user_inputs = state.get("request_data_user_inputs") or {}

    request_data_user_inputs.update(parse_request_data_from_payload(payload))
    request_data_user_inputs.update(parse_request_data_from_message(message))

    waiting_field = state.get("waiting_for_field")

    if waiting_field == CLIENT_APP_ID_SLOT and not client_app_id:
        if is_skip_value_message(message):
            state["client_app_id"] = state.get("generated_client_app_id") or build_client_app_id()
        elif message:
            state["client_app_id"] = message.strip()

        if state.get("client_app_id"):
            state["waiting_for_field"] = None

    elif waiting_field in USER_REQUIRED_REQUEST_FIELDS:
        parsed_current = parse_request_data_from_message(message)

        if waiting_field in parsed_current:
            request_data_user_inputs[waiting_field] = parsed_current[waiting_field]
            state["waiting_for_field"] = None
        elif is_skip_value_message(message):
            request_data_user_inputs[waiting_field] = get_default_step1_value(waiting_field)
            state["waiting_for_field"] = None
        elif message:
            request_data_user_inputs[waiting_field] = normalize_value(waiting_field, message)
            state["waiting_for_field"] = None

    state["request_data_user_inputs"] = request_data_user_inputs
    save_agent_state(request, state)

    if not state.get("client_app_id"):
        state["waiting_for_field"] = CLIENT_APP_ID_SLOT
        save_agent_state(request, state)

        return {
            "ok": True,
            "intent": "zypl_cashflow_scoring",
            "tool": "zypl_cashflow_scoring_agent",
            "status": "need_more_input",
            "answer": build_loan_application_id_question(state),
            "next_field": "loan_application_id",
            "missing_fields": ["loan_application_id"] + find_missing_user_fields(request_data_user_inputs),
            "collected_data": request_data_user_inputs,
            "system_default_extra_fields": SYSTEM_DEFAULT_EXTRA_FIELDS,
            "required_role": "cashflow",
            "public": False,
            "login_required": True,
        }

    missing_fields = find_missing_user_fields(request_data_user_inputs)

    if missing_fields:
        next_field = missing_fields[0]
        state["waiting_for_field"] = next_field
        save_agent_state(request, state)

        return {
            "ok": True,
            "intent": "zypl_cashflow_scoring",
            "tool": "zypl_cashflow_scoring_agent",
            "status": "need_more_input",
            "answer": build_question_for_field(next_field, request_data_user_inputs),
            "next_field": next_field,
            "missing_fields": missing_fields,
            "client_app_id": state["client_app_id"],
            "collected_data": request_data_user_inputs,
            "system_default_extra_fields": SYSTEM_DEFAULT_EXTRA_FIELDS,
            "required_role": "cashflow",
            "public": False,
            "login_required": True,
        }

    full_request_data = build_full_request_data(request_data_user_inputs, state["client_app_id"])
    group_scoring = bool(state.get("group_scoring", False))

    try:
        raw_response = zypl_score(
            client_app_id=state["client_app_id"],
            request_data=full_request_data,
            group_scoring=group_scoring,
        )

        summary = summarize_zypl_response(raw_response)
        answer = build_answer_text(summary)

        used_payload = build_zypl_api_payload(
            client_app_id=state["client_app_id"],
            request_data=full_request_data,
            group_scoring=group_scoring,
        )

        clear_agent_state(request)

        return {
            "ok": True,
            "intent": "zypl_cashflow_scoring",
            "tool": "zypl_cashflow_scoring_agent",
            "status": "scored",
            "answer": answer,
            "summary": summary,
            "zypl_payload_used": used_payload,
            "raw_response": raw_response,
            "required_role": "cashflow",
            "public": False,
            "login_required": True,
        }

    except ZyplAPIError as exc:
        return {
            "ok": False,
            "intent": "zypl_cashflow_scoring",
            "tool": "zypl_cashflow_scoring_agent",
            "status": "error",
            "answer": str(exc),
            "zypl_payload_that_would_be_sent": build_zypl_api_payload(
                client_app_id=state["client_app_id"],
                request_data=full_request_data,
                group_scoring=group_scoring,
            ),
            "required_role": "cashflow",
            "public": False,
            "login_required": True,
        }

    except Exception as exc:
        logger.exception("Unexpected Zypl scoring error")

        return {
            "ok": False,
            "intent": "zypl_cashflow_scoring",
            "tool": "zypl_cashflow_scoring_agent",
            "status": "error",
            "answer": f"Unexpected Zypl scoring error: {exc}",
            "required_role": "cashflow",
            "public": False,
            "login_required": True,
        }


def should_route_to_zypl(request, user_message: str) -> bool:
    if request.session.get(SESSION_KEY):
        return True

    return is_zypl_intent(user_message)


def try_handle_zypl_agent(request, user_message: str) -> Optional[Dict[str, Any]]:
    if not should_route_to_zypl(request, user_message):
        return None

    return process_zypl_agent(request, {"message": user_message})


def zypl_capability_text() -> str:
    return (
        "Zypl Cashflow Scoring Agent: cashflow role/group user Step 1 fields দেবে, "
        "system extra API fields default value দিয়ে full Zypl payload তৈরি করে scoring endpoint call করবে।"
    )
