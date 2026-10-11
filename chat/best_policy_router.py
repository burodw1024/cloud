from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


# =========================================================
# Basic helpers
# =========================================================
def clean(v: Any) -> str:
    return "" if v is None else str(v).strip()


def norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", clean(s)).strip()


def bn_to_en_digits(text: str) -> str:
    return (text or "").translate(str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789"))


def normalize_for_match(text: str) -> str:
    s = clean(text).lower()

    s = s.replace("–", "-").replace("—", "-").replace("_", " ")
    s = s.replace("ঃ", " ").replace("(", " ").replace(")", " ")
    s = s.replace("/", " ").replace("\\", " ").replace(",", " ")
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')

    s = s.replace("চাকুরী", "চাকরি")
    s = s.replace("ছুটী", "ছুটি")
    s = s.replace("বদলী", "বদলি")
    s = s.replace("স্বয়ংক্রিয়", "স্বয়ংক্রিয়")
    s = s.replace("হারাসমেন্ট", "হয়রানি")
    s = s.replace("harasment", "harassment")
    s = s.replace("over time", "overtime")
    s = s.replace("grade stracture", "grade structure")
    s = s.replace("গ্র্যাচুইটি", "গ্র্যাচুয়িটি")
    s = s.replace("gratuaty", "gratuity")
    s = s.replace("providentfund", "provident fund")

    s = s.replace("project staff", "project employee")
    s = s.replace("project employees", "project employee")
    s = s.replace("project worker", "project employee")
    s = s.replace("project workers", "project employee")
    s = s.replace("general employee recruitments", "general employee recruitment")
    s = s.replace("general employees recruitment", "general employee recruitment")
    s = s.replace("employee recruitments", "employee recruitment")
    s = s.replace("staff recruitments", "staff recruitment")
    s = s.replace("employee facilities such as telephone", "telephone facility")
    s = s.replace("employee facility such as telephone", "telephone facility")

    s = s.replace("প্রকল্প স্টাফ", "প্রকল্প কর্মী")
    s = s.replace("প্রজেক্ট স্টাফ", "প্রকল্প কর্মী")
    s = s.replace("প্রজেক্ট কর্মী", "প্রকল্প কর্মী")
    s = s.replace("প্রজেক্ট নিয়োগ", "প্রকল্প নিয়োগ")
    s = s.replace("ই মেইল", "ইমেইল")
    s = s.replace("ই-মেইল", "ইমেইল")

    s = bn_to_en_digits(s)
    s = re.sub(r"[^0-9a-z\u0980-\u09ff\-\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def has_any(q: str, terms: List[str]) -> bool:
    return any(normalize_for_match(t) in q for t in terms)


def rewrite_human_question(question_raw: str) -> str:
    return norm_space(question_raw)


# =========================================================
# Follow-up session helpers
# =========================================================
FOLLOWUP_TERMS = {
    "more", "details", "detail", "full", "complete", "show all",
    "আরও", "বিস্তারিত", "আরো", "সব", "সম্পূর্ণ", "পুরোটা",
}


def is_followup_request(question: str) -> bool:
    return normalize_for_match(question) in FOLLOWUP_TERMS


# =========================================================
# High-priority semantic helpers
# =========================================================
PROJECT_RECRUITMENT_TERMS = [
    "project",
    "project employee",
    "project recruitment",
    "project recruitment policy",
    "project employee recruitment",
    "project staff recruitment",
    "project staff",
    "project employee age limit",
    "project appointment letter",
    "project recruitment committee",
    "project written exam",
    "project oral exam",
    "প্রকল্প",
    "প্রকল্প কর্মী",
    "প্রকল্প কর্মীদের নিয়োগ",
    "প্রকল্প কর্মী নিয়োগ",
    "প্রকল্প নিয়োগ",
    "প্রকল্প কর্মীদের নিয়োগের নিয়মাবলী",
    "প্রকল্প কর্মী নিয়োগের নিয়মাবলী",
    "প্রকল্প কর্মী নিয়োগ কমিটি",
    "প্রকল্প কর্মীদের নিয়োগ কমিটি",
    "প্রকল্প কর্মীর বয়সসীমা",
    "প্রকল্প কর্মীদের বয়সসীমা",
    "প্রকল্প কর্মীর নিয়োগপত্র",
    "প্রকল্প কর্মীদের নিয়োগপত্র",
    "প্রকল্প বাস্তবায়নাধীন বিভাগ",
    "চাহিদাপত্র মানব সম্পদ ব্যবস্থাপনা বিভাগে",
]

GENERAL_RECRUITMENT_TERMS = [
    "general employee recruitment",
    "general recruitment",
    "employee recruitment",
    "staff recruitment",
    "recruitment policy",
    "employee recruitment policy",
    "staff recruitment policy",
    "general employee recruitment policy",
    "general employee",
    "general staff recruitment",
    "কর্মী নিয়োগ",
    "সাধারণ কর্মী নিয়োগ",
    "নিয়োগ নীতিমালা",
    "কর্মী নিয়োগের নীতিমালা",
    "নিয়োগের নীতিমালা",
]

COMPARE_TERMS = [
    "difference",
    "compare",
    "comparison",
    "vs",
    "versus",
    "different",
    "পার্থক্য",
    "তুলনা",
    "ভিন্নতা",
]

COMM_FACILITY_TERMS = [
    "telephone", "telephone facility", "telephone facilities",
    "internet", "internet facility", "internet facilities",
    "email", "email facility", "email facilities",
    "e-mail", "e-mail facility", "e mail", "e mail facility",
    "mobile phone", "phone bill", "internet bill",
    "communication facility", "employee telephone facility",
    "employee internet facility", "employee email facility",
    "staff telephone facility", "staff internet facility", "staff email facility",
    "টেলিফোন", "টেলিফোন সুবিধা",
    "ইন্টারনেট", "ইন্টারনেট সুবিধা",
    "ইমেইল", "ইমেইল সুবিধা",
    "মোবাইল ফোন", "ফোন বিল",
    "কর্মীদের টেলিফোন সুবিধা", "কর্মীদের ইন্টারনেট সুবিধা", "কর্মীদের ইমেইল সুবিধা",
    "সুবিধা", "সুবিধাসমূহ",
]


def is_project_recruitment_question(question: str) -> bool:
    q = normalize_for_match(question)
    return has_any(q, PROJECT_RECRUITMENT_TERMS)


def is_general_recruitment_question(question: str) -> bool:
    q = normalize_for_match(question)
    if has_any(q, PROJECT_RECRUITMENT_TERMS):
        return False
    if has_any(q, COMM_FACILITY_TERMS):
        return False
    return has_any(q, GENERAL_RECRUITMENT_TERMS)


def is_compare_question(question: str) -> bool:
    q = normalize_for_match(question)
    return has_any(q, COMPARE_TERMS)


def is_compare_general_vs_project_recruitment(question: str) -> bool:
    q = normalize_for_match(question)
    if not is_compare_question(q):
        return False
    return has_any(q, GENERAL_RECRUITMENT_TERMS) and has_any(q, PROJECT_RECRUITMENT_TERMS)


def is_comm_facility_question(question: str) -> bool:
    q = normalize_for_match(question)
    return has_any(q, COMM_FACILITY_TERMS)


# =========================================================
# Old manual topic rules
# =========================================================
INTENT_ALIASES: Dict[str, List[str]] = {
    "grade_structure": [
        "grade structure",
        "employee grade structure",
        "grade wise designation",
        "16 grade structure",
        "গ্রেড কাঠামো",
        "১৬টি গ্রেড",
        "গ্রেডভিত্তিক পদ",
        "পদ বিন্যাস",
        "grade chart",
        "designation by grade",
    ],
    "promotion_procedure": [
        "মৌখিক পরীক্ষায় নম্বর",
        "মৌখিক পরীক্ষার নম্বর",
        "নম্বর কীভাবে গণনা",
        "নম্বর গড়",
        "গড় করে নম্বর",
        "গোপন নম্বরপত্র",
        "বোর্ডের নম্বর",
        "মৌখিক পরীক্ষা বোর্ড",
        "oral exam marks",
        "average marks",
        "board average marks",
        "promotion procedure",
        "promotion policy",
        "পদোন্নতি পদ্ধতি",
        "পদোন্নতির প্রক্রিয়া",
        "মৌখিক পরীক্ষা",
        "লিখিত পরীক্ষা",
        "গ্রেড-১৫ থেকে গ্রেড-১৪ পদোন্নতি",
        "গ্রেড ১৫ থেকে গ্রেড ১৪ পদোন্নতি",
        "স্বয়ংক্রিয় পদোন্নতি",
        "automatic promotion",
        "grade 15 to grade 14 promotion",
        "promotion from grade 15 to 14",
        "লিখিত বা মৌখিক পরীক্ষার প্রয়োজন হবে না",
        "no written or oral exam required",
    ],
    "recruitment_policy": [
        "recruitment policy",
        "employee recruitment policy",
        "staff recruitment policy",
        "general employee recruitment",
        "general recruitment",
        "employee recruitment",
        "staff recruitment",
        "general employee recruitment policy",
        "age limit for recruitment",
        "rejoin after resignation policy",
        "resignation rejoin",
        "চাকরি ছেড়ে দিলে আবার যোগদান",
        "পদত্যাগের পর পুনরায় নিয়োগ",
        "কর্মী নিয়োগের নীতিমালা",
        "সাধারণ কর্মী নিয়োগ",
        "কর্মী নিয়োগ",
        "নিয়োগ নীতিমালা",
        "নিয়োগের নীতিমালা",
        "মূল সনদ",
        "মূল সনদপত্র",
        "ফটোকপি",
        "original certificate",
        "certificate submission",
        "নিয়োগের আগে সনদ",
        "বয়সসীমা",
        "age limit",
    ],
    "transfer_policy": [
        "transfer policy",
        "employee transfer policy",
        "staff transfer policy",
        "বদলী নীতিমালা",
        "বদলি নীতিমালা",
        "transfer rules",
        "posting transfer",
    ],
    "disciplinary_policy_full": [
        "punishable offences, types of penalties, and disciplinary measures",
        "punishable offences",
        "types of penalties",
        "disciplinary measures",
        "disciplinary policy",
        "দন্ডযোগ্য অপরাধ, দন্ডের প্রকার ও শাস্তিমূলক ব্যবস্থা",
        "শাস্তিমূলক ব্যবস্থা",
        "ধর্ষণ",
        "ধর্ষণের চেষ্টা",
        "rape",
        "attempt to rape",
        "চূড়ান্ত অব্যাহতি",
        "আইনে সোপর্দ",
        "যৌন হয়রানির শাস্তি",
        "দন্ডযোগ্য অপরাধ",
    ],
    "performance_evaluation": [
        "annual performance appraisal",
        "performance evaluation",
        "employee performance evaluation",
        "employee performance evaluation procedure",
        "who evaluates grade 1",
        "grade 1 evaluation",
        "গ্রেড-১ এর মূল্যায়ন কে করবে",
        "গ্রেড ১ এর মূল্যায়ন",
        "appraisal",
        "annual appraisal",
        "কর্মীর দক্ষতা মূল্যায়ন পদ্ধতি",
        "কর্মদক্ষতা মূল্যায়ন",
        "বাৎসরিক মূল্যায়ন",
        "বার্ষিক কর্ম মূল্যায়ন",
        "বার্ষিক কর্মমূল্যায়ন",
        "কবে appraisal হবে",
        "কবে annual performance appraisal হবে",
        "কবে করতে হবে",
    ],
    "educational_qualification": [
        "educational qualification",
        "educational qualifications by employee grade",
        "grade 14 qualification",
        "grade 15 qualification",
        "গ্রেড-১৪ পদের শিক্ষাগত যোগ্যতা",
        "গ্রেড-১৫ পদের শিক্ষাগত যোগ্যতা",
        "শিক্ষাগত যোগ্যতা",
        "ন্যূনতম শিক্ষাগত যোগ্যতা",
        "গ্রেড অনুযায়ী শিক্ষাগত যোগ্যতা",
        "গ্রেড-১৫ পদের জন্য ন্যূনতম শিক্ষাগত যোগ্যতা",
        "গ্রেড-১৪ পদের জন্য ন্যূনতম শিক্ষাগত যোগ্যতা",
        "গ্রেড-১৩ এবং গ্রেড-১৪ পদের জন্য ন্যূনতম শিক্ষাগত যোগ্যতা",
    ],
}

MANUAL_TOPIC_RULES: List[Dict[str, str]] = [
    {"key": "grade_structure", "topic_id_prefix": "policy_1_3", "topic_bn": "গ্রেড কাঠামো", "topic_en": "Grade Structure"},
    {"key": "promotion_procedure", "topic_id_prefix": "policy_2_3", "topic_bn": "পদোন্নতি পদ্ধতি", "topic_en": "Promotion Procedure"},
    {"key": "recruitment_policy", "topic_id_prefix": "policy_1_5", "topic_bn": "কর্মী নিয়োগের নীতিমালা", "topic_en": "Recruitment Policy"},
    {"key": "transfer_policy", "topic_id_prefix": "policy_1_8", "topic_bn": "বদলী নীতিমালা", "topic_en": "Transfer Policy"},
    {"key": "disciplinary_policy_full", "topic_id_prefix": "policy_3_2_3_6_13", "topic_bn": "দন্ডযোগ্য অপরাধ, দন্ডের প্রকার ও শাস্তিমূলক ব্যবস্থা", "topic_en": "Punishable Offences, Types of Penalties, and Disciplinary Measures"},
    {"key": "performance_evaluation", "topic_id_prefix": "policy_2_4", "topic_bn": "কর্মীর দক্ষতা মূল্যায়ন পদ্ধতি", "topic_en": "Employee Performance Evaluation Procedure"},
    {"key": "educational_qualification", "topic_id_prefix": "policy_1_4", "topic_bn": "কর্মীদের পদ বিন্যাস অনুযায়ী শিক্ষাগত যোগ্যতা", "topic_en": "Educational Qualifications by Employee Grade"},
]

TOPIC_TEXT_CHECKS: Dict[str, List[str]] = {
    "grade_structure": [
        "গ্রেড কাঠামো", "১৬টি গ্রেড", "পদ বিন্যাস", "গ্রেড-১", "গ্রেড-২", "গ্রেড-১৬",
        "নির্বাহী পরিচালক", "পরিচালক", "grade structure",
    ],
    "promotion_procedure": [
        "পদোন্নতি পদ্ধতি", "লিখিত পরীক্ষা", "মৌখিক পরীক্ষা", "বোর্ড",
        "গোপন নম্বর", "নম্বর গড়", "বোর্ডের সদস্যগণ পৃথকভাবে",
        "oral examination", "average marks", "oral exam marks",
        "গ্রেড-১৫ থেকে গ্রেড-১৪ পদে পদোন্নতির জন্য কোন লিখিত কিংবা মৌখিক পরীক্ষার প্রয়োজন হবে না",
        "লিখিত কিংবা মৌখিক পরীক্ষার প্রয়োজন হবে না", "grade-15 to grade-14", "no written or oral exam",
    ],
    "recruitment_policy": [
        "কর্মী নিয়োগের নীতিমালা", "মূল সনদ", "মূল সনদপত্র", "ফটোকপি",
        "শিক্ষাগত যোগ্যতার মূল সনদ", "জমা দিতে হবে", "original certificate",
        "photocopy", "recruitment", "বয়সসীমা", "পুনরায় সংস্থায় চাকুরী করার জন্য যোগ্য",
    ],
    "transfer_policy": [
        "বদলী নীতিমালা", "বদলি নীতিমালা", "transfer", "স্থানান্তর", "posting",
    ],
    "disciplinary_policy_full": [
        "দন্ডযোগ্য অপরাধ", "শাস্তিমূলক ব্যবস্থা", "ধর্ষণ", "ধর্ষণের প্রচেষ্টা",
        "চূড়ান্ত অব্যাহতি", "আইনে সোপর্দ", "rape", "attempt to rape", "final discharge",
    ],
    "performance_evaluation": [
        "কর্মীর দক্ষতা মূল্যায়ন পদ্ধতি", "কর্ম দক্ষতা মূল্যায়ন",
        "বাৎসরিক কর্ম দক্ষতার মূল্যায়ন", "annual performance appraisal",
        "কর্মী মূল্যায়ন নির্দেশিকা", "বৎসর শেষ হওয়ার কমপক্ষে এক মাস পূর্বে",
        "গর্ভধারণকে নেতিবাচক হিসাবে দেখা যাবে না", "গ্রেড-১ এর মূল্যায়ন",
    ],
    "educational_qualification": [
        "শিক্ষাগত যোগ্যতা", "ন্যূনতম শিক্ষাগত যোগ্যতা", "স্নাতকোত্তর", "স্নাতক",
        "উচ্চ মাধ্যমিক", "মাধ্যমিক", "গ্রেড-১৩ এবং গ্রেড-১৪", "গ্রেড-১৫",
    ],
}

PREFIX_TOPIC_KEYS = {
    "grade_structure",
    "promotion_procedure",
    "recruitment_policy",
    "transfer_policy",
    "disciplinary_policy_full",
    "performance_evaluation",
    "educational_qualification",
}

TOPIC_PRIORITY = [
    "performance_evaluation",
    "educational_qualification",
    "recruitment_policy",
    "transfer_policy",
    "disciplinary_policy_full",
    "promotion_procedure",
    "grade_structure",
]

# =========================================================
# Service / project rules topic rules
# =========================================================
SERVICE_RULES_RULES: List[Dict[str, Any]] = [
    {
        "topic_id": "service_rules_6_2",
        "topic_bn": "গ্র্যাচুয়িটি",
        "topic_en": "Gratuity",
        "aliases": [
            "গ্র্যাচুয়িটি", "গ্র্যাচুইটি", "gratuity",
            "গ্র্যাচুয়িটি কীভাবে হিসাব হবে", "gratuity calculation",
            "সর্বশেষ মূল বেতন", "5 বৎসর গ্র্যাচুয়িটি", "12 বৎসর গ্র্যাচুয়িটি", "20 বৎসর গ্র্যাচুয়িটি",
        ],
    },
    {
        "topic_id": "service_rules_6_3",
        "topic_bn": "প্রভিডেন্ট ফান্ড",
        "topic_en": "Provident Fund",
        "aliases": [
            "প্রভিডেন্ট ফান্ড", "provident fund", "pf",
            "10% জমা", "25% জমা", "trustee board",
        ],
    },
    {
        "topic_id": "service_rules_6_4",
        "topic_bn": "আয়কর",
        "topic_en": "Income Tax",
        "aliases": [
            "আয়কর", "income tax", "কর নির্ধারণী", "ট্যাক্স",
        ],
    },
    {
        "topic_id": "service_rules_6_5",
        "topic_bn": "দৈনন্দিন যাতায়াত ভাতা",
        "topic_en": "Daily Travel Allowance",
        "aliases": [
            "যাতায়াত ভাতা", "travel allowance", "দৈনন্দিন যাতায়াত ভাতা",
            "রিক্সা", "সিএনজি", "বিমান ভাড়া", "বাস ভাড়া",
        ],
    },
    {
        "topic_id": "service_rules_6_6",
        "topic_bn": "খাদ্য, অবস্থান ও দৈনিক ভাতা",
        "topic_en": "Food, Lodging and Daily Allowance",
        "aliases": [
            "খাদ্য ভাতা", "অবস্থান ভাতা", "দৈনিক ভাতা",
            "food allowance", "lodging allowance", "daily allowance",
            "বিদেশ ভাতা",
        ],
    },
    {
        "topic_id": "service_rules_6_7",
        "topic_bn": "অধিকাল ভাতা",
        "topic_en": "Overtime Allowance",
        "aliases": [
            "অধিকাল ভাতা", "overtime", "ot", "প্রতি ঘন্টা",
        ],
    },
    {
        "topic_id": "service_rules_6_8",
        "topic_bn": "দূরত্ব ভাতা",
        "topic_en": "Distance Allowance",
        "aliases": [
            "দূরত্ব ভাতা", "distance allowance",
        ],
    },
    {
        "topic_id": "service_rules_6_9",
        "topic_bn": "বদলি/স্থানান্তর ভাতা",
        "topic_en": "Transfer Allowance",
        "aliases": [
            "বদলি ভাতা", "স্থানান্তর ভাতা", "transfer allowance",
        ],
    },
    {
        "topic_id": "service_rules_6_10",
        "topic_bn": "অফিসিয়াল আপ্যায়ন",
        "topic_en": "Official Entertainment",
        "aliases": [
            "অফিসিয়াল আপ্যায়ন", "official entertainment",
        ],
    },
    {
        "topic_id": "service_rules_6_11",
        "topic_bn": "টেলিফোন , ইন্টারনেট ও ই-মেইল সুবিধা",
        "topic_en": "Telephone, Internet and Email Facilities",
        "aliases": [
            "telephone facility",
            "telephone facilities",
            "internet facility",
            "internet facilities",
            "email facility",
            "email facilities",
            "e mail facility",
            "e-mail facility",
            "mobile phone",
            "phone bill",
            "internet bill",
            "employee telephone facility",
            "employee internet facility",
            "employee email facility",
            "employee facilities such as telephone",
            "staff telephone facility",
            "staff internet facility",
            "staff email facility",
            "communication facility",
            "টেলিফোন সুবিধা",
            "টেলিফোন",
            "ইন্টারনেট সুবিধা",
            "ইন্টারনেট",
            "ইমেইল সুবিধা",
            "ইমেইল",
            "মোবাইল ফোন",
            "ফোন বিল",
            "কর্মীদের টেলিফোন সুবিধা",
            "কর্মীদের ইন্টারনেট সুবিধা",
            "কর্মীদের ইমেইল সুবিধা",
        ],
    },
    {
        "topic_id": "service_rules_6_12",
        "topic_bn": "যানবাহন ব্যবহার",
        "topic_en": "Vehicle Usage",
        "aliases": [
            "যানবাহন ব্যবহার", "vehicle usage", "গাড়ী ব্যবহার",
        ],
    },
    {
        "topic_id": "service_rules_6_13",
        "topic_bn": "কর্মী স্বাস্থ্য /চিকিৎসা তহবিল",
        "topic_en": "Employee Health / Medical Fund",
        "aliases": [
            "স্বাস্থ্য তহবিল", "চিকিৎসা তহবিল", "medical fund", "health fund",
            "heart ring", "ক্যান্সার সহায়তা", "কিডনী সহায়তা",
        ],
    },
    {
        "topic_id": "service_rules_6_14",
        "topic_bn": "দুর্ঘটনা জনিত সুবিধা",
        "topic_en": "Accident Benefits",
        "aliases": [
            "দুর্ঘটনা জনিত সুবিধা", "accident benefit",
        ],
    },
    {
        "topic_id": "service_rules_6_15",
        "topic_bn": "কর্মী নিরাপত্তা তহবিল",
        "topic_en": "Employee Security Fund",
        "aliases": [
            "নিরাপত্তা তহবিল", "security fund", "nominee", "মনোনীত ব্যক্তি",
        ],
    },
    {
        "topic_id": "service_rules_6_16",
        "topic_bn": "গৃহ নির্মাণ/ গৃহ পুনঃ নির্মাণ বা ফ্ল্যাট ক্রয়/ফ্ল্যাট নির্মাণ ঋণ",
        "topic_en": "House Construction / Reconstruction / Flat Purchase Loan",
        "aliases": [
            "গৃহ নির্মাণ ঋণ", "ফ্ল্যাট ঋণ", "house loan", "flat loan",
        ],
    },
    {
        "topic_id": "project_staff_7_2",
        "topic_bn": "প্রকল্প কর্মীদের নিয়োগের নিয়মাবলী",
        "topic_en": "Recruitment Rules for Project Employees",
        "aliases": [
            "project recruitment",
            "project employee recruitment",
            "project staff recruitment",
            "project recruitment policy",
            "project employee",
            "project staff",
            "project recruitment committee",
            "project written exam",
            "project oral exam",
            "project employee age limit",
            "project appointment letter",
            "project staff age limit",
            "project staff appointment letter",
            "প্রকল্প কর্মী",
            "প্রকল্প কর্মীদের নিয়োগ",
            "প্রকল্প কর্মী নিয়োগ",
            "প্রকল্প নিয়োগ",
            "প্রকল্প কর্মীদের নিয়োগের নিয়মাবলী",
            "প্রকল্প কর্মী নিয়োগের নিয়মাবলী",
            "প্রকল্প কর্মী নিয়োগ কমিটি",
            "প্রকল্প কর্মীদের নিয়োগ কমিটি",
            "প্রকল্প কর্মীর বয়সসীমা",
            "প্রকল্প কর্মীদের বয়সসীমা",
            "প্রকল্প কর্মীর নিয়োগপত্র",
            "প্রকল্প কর্মীদের নিয়োগপত্র",
            "প্রকল্প কর্মীর লিখিত পরীক্ষা",
            "প্রকল্প কর্মীদের লিখিত পরীক্ষা",
            "প্রকল্প কর্মীর মৌখিক পরীক্ষা",
            "প্রকল্প কর্মীদের মৌখিক পরীক্ষা",
            "প্রকল্প বাস্তবায়নাধীন বিভাগ",
            "চাহিদাপত্র মানব সম্পদ ব্যবস্থাপনা বিভাগে",
            "নির্বাহী পরিচালক কর্তৃক ইস্যু হবে",
        ],
    },



{
    "topic_id": "service_rules_6_17",
    "topic_bn": "গাড়ী ঋণ",
    "topic_en": "Vehicle Loan",
    "aliases": [
        "vehicle loan",
        "car loan",
        "vehicle loan rules",
        "vehicle loan policy",
        "car loan rules",
        "car loan policy",
        "গাড়ী ঋণ",
        "গাড়ি ঋণ",
        "গাড়ী ঋণের নিয়ম",
        "গাড়ী ঋণ নীতিমালা",
    ],
},
]


# =========================================================
# Broad family intents
# =========================================================
def detect_benefit_family_intent(question: str) -> Optional[Dict[str, Any]]:
    q = normalize_for_match(question)

    if has_any(q, [
        "চাকরি শেষে সুবিধা",
        "চাকরি শেষ হলে কী সুবিধা",
        "কর্ম অবসান হলে কী সুবিধা",
        "চাকরি শেষে কী কী পাওয়া যাবে",
        "benefits after leaving job",
        "end of service benefits",
        "separation benefits",
        "retirement benefits",
    ]):
        return {
            "intent_type": "benefit_family",
            "family": "end_of_service_benefits",
            "topic_ids": [
                "service_rules_6_2",
                "service_rules_6_3",
                "service_rules_6_15",
            ],
            "topic_bn": "চাকরি শেষে প্রাপ্য সুবিধাসমূহ",
            "topic_en": "End-of-Service Benefits",
        }

    return None


def detect_compare_intent(question: str) -> Optional[Dict[str, Any]]:
    q = normalize_for_match(question)

    if is_compare_general_vs_project_recruitment(q):
        return {
            "intent_type": "compare_topics",
            "family": "general_vs_project_recruitment",
            "topic_ids": ["policy_1_5", "project_staff_7_2"],
            "topic_bn": "সাধারণ কর্মী নিয়োগ বনাম প্রকল্প কর্মী নিয়োগ",
            "topic_en": "Employee Recruitment vs Project Employee Recruitment",
        }

    return None


def score_manual_rule(question: str, rule: Dict[str, str]) -> int:
    q = normalize_for_match(question)
    key = clean(rule.get("key"))
    aliases = INTENT_ALIASES.get(key, [])
    score = 0

    for alias in aliases:
        a = normalize_for_match(alias)
        if not a:
            continue
        if q == a:
            score += 200
        elif a in q:
            score += 80
        else:
            overlap = set(q.split()) & set(a.split())
            score += len(overlap) * 10

    if key == "performance_evaluation" and has_any(q, [
        "who evaluates grade 1", "grade 1 evaluation", "গ্রেড-১ এর মূল্যায়ন কে করবে", "গ্রেড ১ এর মূল্যায়ন"
    ]):
        score += 400

    if key == "educational_qualification" and has_any(q, [
        "grade 14 qualification", "grade 15 qualification", "গ্রেড-১৪ পদের শিক্ষাগত যোগ্যতা",
        "গ্রেড-১৫ পদের শিক্ষাগত যোগ্যতা", "শিক্ষাগত যোগ্যতা", "ন্যূনতম শিক্ষাগত যোগ্যতা"
    ]):
        score += 350

    if key == "recruitment_policy" and has_any(q, [
        "age limit for recruitment", "বয়সসীমা", "rejoin after resignation policy",
        "পদত্যাগের পর পুনরায় নিয়োগ", "চাকরি ছেড়ে দিলে আবার যোগদান", "recruitment policy"
    ]):
        score += 320

    if key == "recruitment_policy" and is_general_recruitment_question(q):
        score += 500

    if key == "recruitment_policy" and is_compare_general_vs_project_recruitment(q):
        score += 600

    if key == "recruitment_policy" and is_comm_facility_question(q):
        score -= 500

    if key == "grade_structure" and has_any(q, [
        "grade structure", "employee grade structure", "গ্রেড কাঠামো", "১৬টি গ্রেড",
        "গ্রেডভিত্তিক পদ", "পদ বিন্যাস"
    ]):
        score += 220

    if key == "promotion_procedure" and has_any(q, [
        "মৌখিক পরীক্ষায় নম্বর", "মৌখিক পরীক্ষার নম্বর", "নম্বর কীভাবে গণনা",
        "নম্বর গড়", "গোপন নম্বরপত্র", "oral exam marks", "average marks",
        "গ্রেড-১৫ থেকে গ্রেড-১৪", "স্বয়ংক্রিয় পদোন্নতি", "no written or oral exam required"
    ]):
        score += 240

    if key == "transfer_policy" and has_any(q, [
        "transfer policy", "employee transfer policy", "বদলী নীতিমালা", "বদলি নীতিমালা"
    ]):
        score += 220

    if key == "disciplinary_policy_full" and has_any(q, [
        "punishable offences, types of penalties, and disciplinary measures",
        "disciplinary measures", "ধর্ষণ", "ধর্ষণের চেষ্টা", "rape", "attempt to rape"
    ]):
        score += 240

    if key == "recruitment_policy" and is_project_recruitment_question(q) and not is_compare_general_vs_project_recruitment(q):
        score -= 500

    return score


def score_service_rule(question: str, rule: Dict[str, Any]) -> int:
    q = normalize_for_match(question)
    score = 0
    rule_topic_id = clean(rule.get("topic_id"))

    for alias in rule.get("aliases", []):
        a = normalize_for_match(alias)
        if not a:
            continue
        if q == a:
            score += 250
        elif a in q:
            score += 120
        else:
            overlap = set(q.split()) & set(a.split())
            score += len(overlap) * 12

    if rule_topic_id == "project_staff_7_2":
        if has_any(q, ["project", "project staff", "project employee", "প্রকল্প", "প্রকল্প কর্মী"]):
            score += 500
        else:
            score -= 400

        if is_general_recruitment_question(q):
            score -= 700

        if is_compare_general_vs_project_recruitment(q):
            score += 600

        if has_any(q, [
            "recruitment committee",
            "written exam",
            "oral exam",
            "age limit",
            "appointment letter",
            "নিয়োগ কমিটি",
            "লিখিত পরীক্ষা",
            "মৌখিক পরীক্ষা",
            "বয়সসীমা",
            "নিয়োগপত্র",
        ]):
            score += 120

    if rule_topic_id == "service_rules_6_11":
        if has_any(q, [
            "telephone", "internet", "email", "e mail", "e-mail",
            "mobile phone", "phone bill", "internet bill",
            "টেলিফোন", "ইন্টারনেট", "ইমেইল", "মোবাইল ফোন",
        ]):
            score += 600

        if has_any(q, [
            "facility", "facilities", "benefit", "benefits",
            "সুবিধা", "সুবিধাসমূহ",
        ]):
            score += 250

    return score


def resolve_manual_topic(question: str) -> Optional[Dict[str, str]]:
    best_rule: Optional[Dict[str, str]] = None
    best_score = -1

    rules_sorted = sorted(
        MANUAL_TOPIC_RULES,
        key=lambda r: TOPIC_PRIORITY.index(clean(r.get("key"))) if clean(r.get("key")) in TOPIC_PRIORITY else 999
    )

    for rule in rules_sorted:
        s = score_manual_rule(question, rule)
        if s > best_score:
            best_score = s
            best_rule = rule

    return best_rule if best_rule and best_score >= 20 else None


def resolve_service_rule_topic(question: str) -> Optional[Dict[str, str]]:
    best_rule = None
    best_score = -1

    for rule in SERVICE_RULES_RULES:
        s = score_service_rule(question, rule)
        if s > best_score:
            best_score = s
            best_rule = rule

    if best_rule and best_score >= 20:
        return {
            "topic_id": clean(best_rule.get("topic_id")),
            "topic_bn": clean(best_rule.get("topic_bn")),
            "topic_en": clean(best_rule.get("topic_en")),
            "clause_range": "",
            "source_file": "",
            "manual_key": "",
        }
    return None


def detect_best_topic(question: str, collection: str, get_topic_catalog_func) -> Optional[Dict[str, str]]:
    family_intent = detect_benefit_family_intent(question)
    if family_intent:
        return {
            "topic_id": "",
            "topic_bn": family_intent["topic_bn"],
            "topic_en": family_intent["topic_en"],
            "clause_range": "",
            "source_file": "",
            "manual_key": "",
            "family_intent": family_intent["family"],
            "topic_ids": family_intent["topic_ids"],
            "intent_type": family_intent["intent_type"],
        }

    compare_intent = detect_compare_intent(question)
    if compare_intent:
        return {
            "topic_id": "",
            "topic_bn": compare_intent["topic_bn"],
            "topic_en": compare_intent["topic_en"],
            "clause_range": "",
            "source_file": "",
            "manual_key": "",
            "family_intent": compare_intent["family"],
            "topic_ids": compare_intent["topic_ids"],
            "intent_type": compare_intent["intent_type"],
        }

    topics = get_topic_catalog_func(collection)

    service_rule = resolve_service_rule_topic(question)
    if service_rule:
        sr_topic_id = clean(service_rule.get("topic_id"))
        sr_topic_bn = clean(service_rule.get("topic_bn"))
        sr_topic_en = clean(service_rule.get("topic_en"))

        for t in topics:
            t_id = clean(t.get("topic_id"))
            if sr_topic_id and t_id == sr_topic_id:
                out = dict(t)
                out["manual_key"] = ""
                return out
            if sr_topic_bn and clean(t.get("topic_bn")) == sr_topic_bn:
                out = dict(t)
                out["manual_key"] = ""
                return out
            if sr_topic_en and clean(t.get("topic_en")) == sr_topic_en:
                out = dict(t)
                out["manual_key"] = ""
                return out

        return service_rule

    manual = resolve_manual_topic(question)
    if manual:
        topic_id = clean(manual.get("topic_id"))
        topic_id_prefix = clean(manual.get("topic_id_prefix"))
        topic_bn = clean(manual.get("topic_bn"))
        topic_en = clean(manual.get("topic_en"))

        for t in topics:
            t_id = clean(t.get("topic_id"))
            if topic_id and t_id == topic_id:
                out = dict(t)
                out["manual_key"] = manual.get("key", "")
                return out
            if topic_id_prefix and t_id.startswith(topic_id_prefix):
                out = dict(t)
                out["manual_key"] = manual.get("key", "")
                return out
            if topic_bn and clean(t.get("topic_bn")) == topic_bn:
                out = dict(t)
                out["manual_key"] = manual.get("key", "")
                return out
            if topic_en and clean(t.get("topic_en")) == topic_en:
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

    return None


def build_exact_topic_filter(topic: Optional[Dict[str, str]]) -> Optional[dict]:
    if not topic:
        return None

    topic_id = clean(topic.get("topic_id"))
    topic_bn = clean(topic.get("topic_bn"))
    topic_en = clean(topic.get("topic_en"))
    clause_range = clean(topic.get("clause_range"))
    manual_key = clean(topic.get("manual_key"))
    topic_ids = topic.get("topic_ids") or []

    if topic_ids:
        conditions = []
        for tid in topic_ids:
            conditions.append({"key": "topic_id", "match": {"text": clean(tid)}})
        return {
            "min_should": {
                "conditions": conditions,
                "min_count": 1,
            }
        }

    if topic_id and manual_key in PREFIX_TOPIC_KEYS:
        return {"must": [{"key": "topic_id", "match": {"text": topic_id}}]}

    if topic_id:
        return {"must": [{"key": "topic_id", "match": {"value": topic_id}}]}

    should = []
    if topic_bn:
        should.append({"key": "topic_bn", "match": {"value": topic_bn}})
    if topic_en:
        should.append({"key": "topic_en", "match": {"value": topic_en}})
    if clause_range:
        should.append({"key": "clause_range", "match": {"value": clause_range}})
    return {"should": should} if should else None


def topic_text_matches(hit: dict, detected_topic: Optional[Dict[str, str]]) -> bool:
    if not detected_topic:
        return True

    topic_key = clean(detected_topic.get("manual_key"))
    checks = TOPIC_TEXT_CHECKS.get(topic_key) or []
    if not checks:
        return True

    payload = hit.get("payload") or {}
    blob = " ".join([
        clean(payload.get("topic_bn")),
        clean(payload.get("topic_en")),
        clean(payload.get("clause_range")),
        clean(payload.get("text")),
    ])
    norm = normalize_for_match(blob)
    return any(normalize_for_match(c) in norm for c in checks)


def filter_hits_by_topic_family(hits: List[dict], detected_topic: Optional[Dict[str, str]]) -> List[dict]:
    if not detected_topic:
        return hits

    topic_id = clean(detected_topic.get("topic_id"))
    manual_key = clean(detected_topic.get("manual_key"))
    topic_ids = [clean(x) for x in (detected_topic.get("topic_ids") or []) if clean(x)]

    out: List[dict] = []

    for h in hits:
        payload = h.get("payload") or {}
        hit_topic_id = clean(payload.get("topic_id") or payload.get("id"))

        if topic_ids:
            for wanted in topic_ids:
                if hit_topic_id == wanted or hit_topic_id.startswith(wanted):
                    out.append(h)
                    break
            continue

        if manual_key in PREFIX_TOPIC_KEYS:
            if topic_id and hit_topic_id.startswith(topic_id):
                out.append(h)
            continue

        if topic_id and hit_topic_id == topic_id:
            out.append(h)

    return out


def find_lines_by_terms(ctx: str, terms: List[str], limit: int = 10) -> List[str]:
    lines = [ln.strip() for ln in ctx.splitlines() if ln.strip()]
    out: List[str] = []
    seen: set[str] = set()

    for line in lines:
        norm = normalize_for_match(line)
        if any(normalize_for_match(t) in norm for t in terms):
            k = normalize_for_match(line)
            if k not in seen:
                seen.add(k)
                out.append(line)
        if len(out) >= limit:
            break

    return out


def format_simple_answer(summary: str, bullets: List[str], citations: List[Dict[str, Any]]) -> str:
    src = ", ".join([f"[{c['idx']}]" for c in citations[:5]]) if citations else "[1]"
    out = [f"Summary: {summary}", "Rules:"]
    for b in bullets:
        out.append(f"- {b}")
    out.append(f"Sources: {src}")
    return "\n".join(out)


def format_compare_answer(summary: str, left_title: str, left_points: List[str], right_title: str, right_points: List[str], citations: List[Dict[str, Any]]) -> str:
    src = ", ".join([f"[{c['idx']}]" for c in citations[:8]]) if citations else "[1]"
    out = [f"Summary: {summary}", "Rules:"]
    out.append(f"- {left_title}")
    for p in left_points:
        out.append(f"  - {p}")
    out.append(f"- {right_title}")
    for p in right_points:
        out.append(f"  - {p}")
    out.append(f"Sources: {src}")
    return "\n".join(out)


# =========================================================
# Broad family answers
# =========================================================
def answer_end_of_service_benefits(question_raw: str, ctx: str, citations: List[Dict[str, Any]]) -> Optional[str]:
    q = normalize_for_match(question_raw)

    if not has_any(q, [
        "চাকরি শেষে সুবিধা",
        "চাকরি শেষ হলে কী সুবিধা",
        "কর্ম অবসান হলে কী সুবিধা",
        "চাকরি শেষে কী কী পাওয়া যাবে",
        "benefits after leaving job",
        "end of service benefits",
    ]):
        return None

    bullets = find_lines_by_terms(ctx, [
        "গ্র্যাচুয়িটি",
        "প্রভিডেন্ট ফান্ড",
        "নিরাপত্তা তহবিল",
        "কর্ম অবসান",
        "বিচ্ছেদ",
        "মৃত্যুবরণ",
        "refund",
        "সর্বশেষ মূল বেতন",
    ], 20)

    if bullets:
        return format_simple_answer(
            "নীতিমালার ভিত্তিতে চাকরি শেষে প্রধান সুবিধার মধ্যে গ্র্যাচুয়িটি, প্রভিডেন্ট ফান্ডের প্রাপ্য অংশ বা ফেরত, এবং প্রযোজ্য ক্ষেত্রে কর্মী নিরাপত্তা তহবিলের সুবিধা অন্তর্ভুক্ত হতে পারে। নিচে প্রাসঙ্গিক উদ্ধৃতি দেওয়া হলো।",
            bullets,
            citations,
        )

    return None


def answer_compare_general_vs_project_recruitment(question_raw: str, ctx: str, citations: List[Dict[str, Any]]) -> Optional[str]:
    q = normalize_for_match(question_raw)
    if not is_compare_general_vs_project_recruitment(q):
        return None

    lines = [ln.strip() for ln in ctx.splitlines() if ln.strip()]

    general_points = []
    project_points = []

    for ln in lines:
        n = normalize_for_match(ln)

        if any(x in n for x in [
            normalize_for_match("কর্মী নিয়োগের নীতিমালা"),
            normalize_for_match("জাতীয় দৈনিক পত্রিকায় বিজ্ঞপ্তি"),
            normalize_for_match("গ্রেড-১৩ পর্যন্ত"),
            normalize_for_match("গ্রেড-১৪, গ্রেড-১৫ এবং গ্রেড-১৬"),
            normalize_for_match("বাংলাদেশের যে কোন নাগরিক"),
            normalize_for_match("জেন্ডার সংবেদনশীল"),
            normalize_for_match("জেলা ভিত্তিক সমতা"),
            normalize_for_match("সংযুক্ত-১"),
            normalize_for_match("সংযুক্ত-২"),
            normalize_for_match("সংযুক্ত-৩"),
            normalize_for_match("সংযুক্ত-৪"),
            normalize_for_match("জামানত"),
            normalize_for_match("রেফারেন্স প্রক্রিয়া"),
            normalize_for_match("লিখিত ও মৌখিক প্রক্রিয়া"),
        ]):
            general_points.append(ln)
            continue

        if any(x in n for x in [
            normalize_for_match("প্রকল্প বাস্তবায়নাধীন বিভাগ"),
            normalize_for_match("চাহিদাপত্র মানব সম্পদ ব্যবস্থাপনা বিভাগে"),
            normalize_for_match("প্রযোজ্য ক্ষেত্রে জাতীয় দৈনিক পত্রিকা"),
            normalize_for_match("প্রকল্প কর্মী নিয়োগ কমিটির মাধ্যমে"),
            normalize_for_match("উপ-ধারা ২.৩.৫ অনুসরণ করবে"),
            normalize_for_match("job description"),
            normalize_for_match("১৮ বৎসরের নিচে"),
            normalize_for_match("৬০ বৎসরের উর্ধ্বে"),
            normalize_for_match("নিয়োগপত্র নির্বাহী পরিচালক কর্তৃক ইস্যু হবে"),
        ]):
            project_points.append(ln)
            continue

    if not general_points:
        general_points = [
            "সাধারণ কর্মী নিয়োগে নিয়মিত কর্মী নিয়োগ নীতিমালা প্রযোজ্য হয়।",
            "জাতীয়/স্থানীয় বিজ্ঞপ্তি, যোগ্যতা, নথি, বয়সসীমা, অঙ্গীকারনামা ও নিয়োগ প্রক্রিয়া সাধারণ নীতিমালা অনুযায়ী অনুসৃত হয়।",
            "কিছু পদে জামানত, রেফারেন্স, আত্মীয়স্বজন সংক্রান্ত ঘোষণা এবং নির্দিষ্ট নিয়োগ শর্ত প্রযোজ্য হতে পারে।",
        ]

    if not project_points:
        project_points = [
            "প্রকল্প কর্মী নিয়োগে প্রকল্পভিত্তিক চাহিদাপত্র থেকে নিয়োগ প্রক্রিয়া শুরু হয়।",
            "মানবসম্পদ ব্যবস্থাপনা বিভাগ কর্তৃক গঠিত প্রকল্প কর্মী নিয়োগ কমিটির মাধ্যমে নিয়োগ হয়।",
            "প্রযোজ্য ক্ষেত্রে লিখিত ও মৌখিক পরীক্ষা হয়, বয়স সাধারণভাবে ১৮ থেকে ৬০ বছরের মধ্যে, এবং নিয়োগপত্র নির্বাহী পরিচালক ইস্যু করেন।",
        ]

    return format_compare_answer(
        "সাধারণ কর্মী নিয়োগ ও প্রকল্প কর্মী নিয়োগ এক নয়। সাধারণ কর্মী নিয়োগ নিয়মিত নিয়োগ নীতিমালার অধীনে হয়, আর প্রকল্প কর্মী নিয়োগ প্রকল্পভিত্তিক আলাদা নিয়মে পরিচালিত হয়।",
        "সাধারণ কর্মী নিয়োগ",
        general_points[:6],
        "প্রকল্প কর্মী নিয়োগ",
        project_points[:6],
        citations,
    )


# =========================================================
# Deterministic topic answers
# =========================================================
def answer_service_rules_gratuity(question_raw: str, ctx: str, citations: List[Dict[str, Any]]) -> Optional[str]:
    q = normalize_for_match(question_raw)

    bullets: List[str] = []

    if has_any(q, ["কীভাবে হিসাব হবে", "হিসাব", "গণনা", "calculation", "calculate"]):
        bullets = find_lines_by_terms(ctx, [
            "৫ বৎসর",
            "সর্বশেষ মূল বেতন",
            "১২ বৎসর",
            "দ্বিগুণ",
            "২০ বৎসর",
            "তিন গুণ",
            "ভগ্ন বর্ষ",
            "ভগ্ন মাস",
        ], 10)

        if bullets:
            return format_simple_answer(
                "নীতিমালায় গ্র্যাচুয়িটি হিসাব এভাবে বলা হয়েছে: ৫ বছর পূর্ণ হলে সর্বশেষ মূল বেতনের ১ গুণ, ১২ বছর পূর্ণ হলে ২ গুণ, ২০ বছর পূর্ণ হলে ৩ গুণ হারে প্রাপ্য হবে; ভগ্ন মাস থাকলে নির্ধারিত নিয়মে হিসাব হবে।",
                bullets,
                citations,
            )

    bullets = find_lines_by_terms(ctx, [
        "গ্র্যাচুয়িটি সুবিধা",
        "৫ বৎসর",
        "১২ বৎসর",
        "২০ বৎসর",
        "ভগ্ন বর্ষ",
    ], 10)

    if bullets:
        return format_simple_answer(
            "নীতিমালায় গ্র্যাচুয়িটি সংক্রান্ত নিয়ম নিচে দেওয়া হলো।",
            bullets,
            citations,
        )

    return None


def answer_comm_facilities(question_raw: str, ctx: str, citations: List[Dict[str, Any]]) -> Optional[str]:
    q = normalize_for_match(question_raw)

    if not is_comm_facility_question(q):
        return None

    bullets = find_lines_by_terms(ctx, [
        "টেলিফোন", "ইন্টারনেট", "ইমেইল", "মোবাইল ফোন", "ফোন বিল", "internet bill"
    ], 12)

    if bullets:
        return format_simple_answer(
            "নীতিমালায় টেলিফোন, ইন্টারনেট ও ইমেইল সুবিধা সংক্রান্ত নিয়ম নিচে দেওয়া হলো।",
            bullets,
            citations,
        )

    return None


def answer_grade_structure(question_raw: str, ctx: str, citations: List[Dict[str, Any]]) -> Optional[str]:
    q = normalize_for_match(question_raw)
    lines = [ln.strip() for ln in ctx.splitlines() if ln.strip()]

    def find_grade_lines(grade_no: str) -> List[str]:
        out = []
        for ln in lines:
            norm = normalize_for_match(bn_to_en_digits(ln))
            if f"গ্রেড-{grade_no}" in norm or f"গ্রেড {grade_no}" in norm or f"grade-{grade_no}" in norm or f"grade {grade_no}" in norm:
                out.append(ln)
        return out

    m = re.search(r"\bgrade[\s\-]*([0-9]{1,2})\b", bn_to_en_digits(q), re.I)
    if not m:
        m = re.search(r"গ্রেড[\s\-]*([0-9]{1,2})", bn_to_en_digits(q))

    if m:
        grade_no = m.group(1)
        bullets = find_grade_lines(grade_no)
        if bullets:
            return format_simple_answer(
                f"নীতিমালায় গ্রেড-{grade_no} এর পদ এভাবে উল্লেখ আছে।",
                bullets,
                citations,
            )
        return format_simple_answer(
            f"নীতিমালার প্রাসঙ্গিক অংশে গ্রেড-{grade_no} এর পদ স্পষ্টভাবে পাওয়া যায়নি।",
            lines[:30],
            citations,
        )

    if is_followup_request(question_raw):
        return format_simple_answer(
            "নীতিমালায় গ্রেড কাঠামোর বিস্তারিত তালিকা নিচে দেওয়া হলো।",
            lines[:40],
            citations,
        )

    return format_simple_answer(
        "নীতিমালায় গ্রেড কাঠামো এভাবে নির্ধারণ করা হয়েছে।",
        lines[:30],
        citations,
    )


def answer_recruitment_policy(question_raw: str, ctx: str, citations: List[Dict[str, Any]]) -> Optional[str]:
    q = normalize_for_match(question_raw)

    if is_project_recruitment_question(q) and not is_compare_general_vs_project_recruitment(q):
        return None

    if is_comm_facility_question(q):
        return None

    if has_any(q, ["age limit for recruitment", "বয়সসীমা", "age limit"]):
        bullets = find_lines_by_terms(ctx, [
            "সর্বোচ্চ বয়সসীমা হবে 35", "সর্বোচ্চ বয়সসীমা হবে ৩৫", "গ্রেড-11 থেকে গ্রেড-16",
            "গ্রেড-১ এর কর্মী নিয়োগের ক্ষেত্রে বয়সসীমা", "গভর্ণিং বডি কর্তৃক নির্ধারিত"
        ], 6)
        if bullets:
            return format_simple_answer(
                "নীতিমালায় নিয়োগের বয়সসীমা এভাবে বলা হয়েছে।",
                bullets,
                citations,
            )

    if has_any(q, ["rejoin after resignation policy", "resignation rejoin", "পদত্যাগের পর পুনরায় নিয়োগ", "চাকরি ছেড়ে দিলে আবার যোগদান"]):
        bullets = find_lines_by_terms(ctx, [
            "স্বেচ্ছায় চাকুরী থেকে অব্যাহতি", "পদত্যাগ করলে", "পুনরায় সংস্থায় চাকুরী করার জন্য যোগ্য",
            "চাকুরী করতে পারবে না"
        ], 4)
        if bullets:
            return format_simple_answer(
                "নীতিমালায় বলা হয়েছে, পদত্যাগ বা চাকুরীচ্যুত হলে পুনরায় সংস্থায় চাকুরীর জন্য যোগ্য গণ্য হবে না।",
                bullets,
                citations,
            )

    if has_any(q, ["মূল সনদ", "মূল সনদপত্র", "ফটোকপি", "original certificate", "certificate", "জমা", "recruitment policy"]):
        bullets = find_lines_by_terms(ctx, [
            "মূল সনদ", "মূল সনদপত্র", "ফটোকপি", "জমা দিতে হবে", "সংরক্ষণ করতে পারবে"
        ], 6)
        if bullets:
            return format_simple_answer(
                "নীতিমালায় মূল সনদপত্র জমা বা প্রদর্শনের নিয়ম এভাবে বলা হয়েছে।",
                bullets,
                citations,
            )

    lines = [ln.strip() for ln in ctx.splitlines() if ln.strip()]
    return format_simple_answer("নীতিমালায় কর্মী নিয়োগের নিয়ম এভাবে বলা হয়েছে।", lines[:20], citations)


def answer_project_staff_recruitment(question_raw: str, ctx: str, citations: List[Dict[str, Any]]) -> Optional[str]:
    q = normalize_for_match(question_raw)
    lines = [ln.strip() for ln in ctx.splitlines() if ln.strip()]

    if has_any(q, ["recruitment committee", "নিয়োগ কমিটি", "কমিটি"]):
        bullets = find_lines_by_terms(ctx, [
            "প্রকল্প কর্মী নিয়োগ কমিটির মাধ্যমে",
            "মানবসম্পদ ব্যবস্থাপনা বিভাগ কর্তৃক গঠিত",
            "নিয়োগ কমিটি",
        ], 6)
        if bullets:
            return format_simple_answer(
                "প্রকল্প কর্মী নিয়োগ মানবসম্পদ ব্যবস্থাপনা বিভাগ কর্তৃক গঠিত প্রকল্প কর্মী নিয়োগ কমিটির মাধ্যমে করতে হবে।",
                bullets,
                citations,
            )

    if has_any(q, ["written exam", "লিখিত পরীক্ষা"]):
        bullets = find_lines_by_terms(ctx, [
            "লিখিত",
            "লিখিত এবং মৌখিক পরীক্ষা",
            "প্রযোজ্য ক্ষেত্রে",
        ], 6)
        if bullets:
            return format_simple_answer(
                "প্রকল্প কর্মী নিয়োগের জন্য প্রযোজ্য ক্ষেত্রে লিখিত পরীক্ষা গ্রহণ করতে হবে।",
                bullets,
                citations,
            )

    if has_any(q, ["oral exam", "মৌখিক পরীক্ষা", "viva"]):
        bullets = find_lines_by_terms(ctx, [
            "মৌখিক পরীক্ষা",
            "লিখিত এবং মৌখিক পরীক্ষা",
            "উপ-ধারা ২.৩.৫ অনুসরণ করবে",
        ], 6)
        if bullets:
            return format_simple_answer(
                "প্রকল্প কর্মী নিয়োগের জন্য প্রযোজ্য ক্ষেত্রে মৌখিক পরীক্ষা গ্রহণ করতে হবে এবং তা নিয়মিত কর্মী নিয়োগের উপ-ধারা ২.৩.৫ অনুসরণ করবে।",
                bullets,
                citations,
            )

    if has_any(q, ["age limit", "বয়সসীমা", "বয়স সীমা"]):
        bullets = find_lines_by_terms(ctx, [
            "১৮ বৎসরের নিচে",
            "৬০ বৎসরের উর্ধ্বে",
            "সাধারণভাবে চাকুরী প্রার্থীদের বয়স",
        ], 6)
        if bullets:
            return format_simple_answer(
                "প্রকল্প কর্মী নিয়োগের ক্ষেত্রে সাধারণভাবে প্রার্থীর বয়স ১৮ বছরের নিচে এবং ৬০ বছরের উর্ধ্বে হবে না।",
                bullets,
                citations,
            )

    if has_any(q, ["appointment letter", "নিয়োগপত্র", "appointment", "চূড়ান্ত নিয়োগ অনুমোদন"]):
        bullets = find_lines_by_terms(ctx, [
            "চূড়ান্ত নিয়োগ অনুমোদন",
            "নিয়োগপত্র নির্বাহী পরিচালক কর্তৃক ইস্যু হবে",
            "নির্বাহী পরিচালক",
        ], 6)
        if bullets:
            return format_simple_answer(
                "প্রকল্প কর্মীর চূড়ান্ত নিয়োগ অনুমোদন ও নিয়োগপত্র নির্বাহী পরিচালক কর্তৃক ইস্যু হবে।",
                bullets,
                citations,
            )

    if has_any(q, ["job description", "শর্তাবলী", "সুযোগ-সুবিধা"]):
        bullets = find_lines_by_terms(ctx, [
            "চাকুরীর শর্তাবলী",
            "সুযোগ-সুবিধা",
            "job description",
            "পরিষ্কার উল্লেখ করতে হবে",
        ], 6)
        if bullets:
            return format_simple_answer(
                "প্রকল্প কর্মী নিয়োগের ক্ষেত্রে চাকুরীর শর্তাবলী, সুযোগ-সুবিধা ও job description পরিষ্কারভাবে উল্লেখ করতে হবে।",
                bullets,
                citations,
            )

    if has_any(q, ["verification", "যাচাই", "নথি যাচাই", "certificate verification"]):
        bullets = find_lines_by_terms(ctx, [
            "জীবন বৃত্তান্ত",
            "মূল সনদপত্র",
            "জাতীয় পরিচয়পত্র",
            "জন্ম নিবন্ধন",
            "সর্বশেষ কর্মস্থলের পত্র",
            "যাচাই করা হবে",
        ], 8)
        if bullets:
            return format_simple_answer(
                "প্রযোজ্য ক্ষেত্রে প্রকল্প কর্মী নিয়োগের সময় জীবন বৃত্তান্ত, মূল সনদপত্র, জাতীয় পরিচয়পত্র/জন্ম নিবন্ধন এবং সর্বশেষ কর্মস্থলের পত্র যাচাই করা হবে।",
                bullets,
                citations,
            )

    return format_simple_answer(
        "নীতিমালায় প্রকল্প কর্মীদের নিয়োগের নিয়মাবলী এভাবে বলা হয়েছে।",
        lines[:20],
        citations,
    )


def answer_transfer_policy(question_raw: str, ctx: str, citations: List[Dict[str, Any]]) -> Optional[str]:
    lines = [ln.strip() for ln in ctx.splitlines() if ln.strip()]
    return format_simple_answer(
        "নীতিমালায় বদলী সংক্রান্ত নিয়ম এভাবে বলা হয়েছে।",
        lines[:20],
        citations,
    )


def answer_promotion_procedure(question_raw: str, ctx: str, citations: List[Dict[str, Any]]) -> Optional[str]:
    q = normalize_for_match(question_raw)
    lines = [ln.strip() for ln in ctx.splitlines() if ln.strip()]

    if has_any(q, [
        "গ্রেড-১৫ থেকে গ্রেড-১৪", "গ্রেড 15 থেকে গ্রেড 14", "স্বয়ংক্রিয় পদোন্নতি",
        "automatic promotion", "grade 15 to grade 14 promotion", "promotion from grade 15 to 14",
    ]):
        bullets = find_lines_by_terms(ctx, [
            "গ্রেড-১৫ থেকে গ্রেড-১৪", "লিখিত কিংবা মৌখিক পরীক্ষার প্রয়োজন হবে না",
            "লিখিত পরীক্ষা", "মৌখিক পরীক্ষা",
        ], 8)
        if bullets:
            return format_simple_answer(
                "নীতিমালায় স্বয়ংক্রিয় পদোন্নতি বলা নেই, তবে গ্রেড-১৫ থেকে গ্রেড-১৪ পদোন্নতিতে লিখিত বা মৌখিক পরীক্ষার প্রয়োজন হবে না।",
                bullets,
                citations,
            )

    if has_any(q, [
        "মৌখিক পরীক্ষায় নম্বর", "মৌখিক পরীক্ষার নম্বর", "নম্বর কীভাবে গণনা",
        "নম্বর গড়", "গোপন নম্বরপত্র", "oral exam marks", "average marks",
    ]):
        bullets = find_lines_by_terms(ctx, [
            "গোপন নম্বর", "পৃথকভাবে", "নম্বর প্রদান করবেন", "নম্বর গড়",
            "প্রত্যেক প্রার্থীর নম্বর নির্ণয় করবে", "স্বাক্ষরসহ নম্বরপত্র",
        ], 10)
        if bullets:
            return format_simple_answer(
                "নীতিমালায় বলা হয়েছে, মৌখিক পরীক্ষা বোর্ডের সদস্যরা আলাদাভাবে নম্বর দেবেন এবং পরে মানবসম্পদ ব্যবস্থাপনা বিভাগ সবার নম্বরের গড় করে চূড়ান্ত নম্বর নির্ধারণ করবে।",
                bullets,
                citations,
            )

    return format_simple_answer("নীতিমালায় পদোন্নতির পদ্ধতি ও বোর্ড কাঠামো এভাবে নির্ধারণ করা হয়েছে।", lines[:20], citations)


def answer_disciplinary_policy_full(question_raw: str, ctx: str, citations: List[Dict[str, Any]]) -> Optional[str]:
    q = normalize_for_match(question_raw)
    lines = [ln.strip() for ln in ctx.splitlines() if ln.strip()]

    if has_any(q, ["ধর্ষণ", "ধর্ষণের চেষ্টা", "rape", "attempt to rape"]):
        bullets = find_lines_by_terms(ctx, [
            "ধর্ষণ", "ধর্ষণের প্রচেষ্টা", "চাকুরী থেকে চূড়ান্ত অব্যাহতি", "আইনে সোপর্দ", "চূড়ান্ত অব্যাহতি",
        ], 8)
        if bullets:
            return format_simple_answer(
                "নীতিমালায় ধর্ষণ বা ধর্ষণের চেষ্টার ক্ষেত্রে শাস্তি এভাবে বলা হয়েছে।",
                bullets,
                citations,
            )

    return format_simple_answer("নীতিমালায় দন্ডযোগ্য অপরাধ ও শাস্তিমূলক ব্যবস্থা এভাবে বলা হয়েছে।", lines[:20], citations)


def answer_performance_evaluation(question_raw: str, ctx: str, citations: List[Dict[str, Any]]) -> Optional[str]:
    q = normalize_for_match(question_raw)

    if has_any(q, ["who evaluates grade 1", "grade 1 evaluation", "গ্রেড-১ এর মূল্যায়ন কে করবে", "গ্রেড ১ এর মূল্যায়ন"]):
        bullets = find_lines_by_terms(ctx, [
            "গ্রেড-১ এর মূল্যায়ন", "চেয়ারপার্সন কর্তৃক", "গভর্ণিং বডির সিদ্ধান্তক্রমে"
        ], 4)
        if bullets:
            return format_simple_answer(
                "নীতিমালায় বলা হয়েছে, গ্রেড-১ এর মূল্যায়ন গভর্ণিং বডির সিদ্ধান্তক্রমে চেয়ারপার্সন কর্তৃক হবে।",
                bullets,
                citations,
            )

    if has_any(q, [
        "annual performance appraisal", "performance evaluation", "annual appraisal",
        "কবে করতে হবে", "কবে appraisal হবে", "কর্মীর দক্ষতা মূল্যায়ন", "কর্মদক্ষতা মূল্যায়ন",
        "বাৎসরিক মূল্যায়ন", "বার্ষিক কর্মমূল্যায়ন",
    ]):
        bullets = find_lines_by_terms(ctx, [
            "বৎসর শেষ হওয়ার কমপক্ষে এক মাস পূর্বে", "annual performance appraisal",
            "কর্মী মূল্যায়ন নির্দেশিকা", "মানব সম্পদ ব্যবস্থাপনা বিভাগ",
        ], 6)
        if bullets:
            return format_simple_answer(
                "নীতিমালায় বলা হয়েছে, annual performance appraisal বৎসর শেষ হওয়ার কমপক্ষে এক মাস পূর্বে করতে হবে।",
                bullets,
                citations,
            )

    lines = [ln.strip() for ln in ctx.splitlines() if ln.strip()]
    return format_simple_answer(
        "নীতিমালায় কর্মীর দক্ষতা মূল্যায়ন পদ্ধতি এভাবে বলা হয়েছে।",
        lines[:14],
        citations,
    )


def answer_educational_qualification(question_raw: str, ctx: str, citations: List[Dict[str, Any]]) -> Optional[str]:
    q = normalize_for_match(question_raw)

    m = re.search(r"\bgrade[\s\-]*([0-9]{1,2})\b", bn_to_en_digits(q), re.I)
    if not m:
        m = re.search(r"গ্রেড[\s\-]*([0-9]{1,2})", bn_to_en_digits(q))

    if m and has_any(q, ["qualification", "শিক্ষাগত যোগ্যতা", "ন্যূনতম শিক্ষাগত যোগ্যতা"]):
        grade_no = m.group(1)
        bullets = find_lines_by_terms(ctx, [
            f"গ্রেড-{grade_no}", f"গ্রেড {grade_no}", "স্নাতকোত্তর", "স্নাতক", "উচ্চ মাধ্যমিক", "মাধ্যমিক"
        ], 6)
        if bullets:
            return format_simple_answer(
                f"নীতিমালায় গ্রেড-{grade_no} পদের শিক্ষাগত যোগ্যতা এভাবে বলা হয়েছে।",
                bullets,
                citations,
            )

    if has_any(q, ["শিক্ষাগত যোগ্যতা", "ন্যূনতম শিক্ষাগত যোগ্যতা", "educational qualification"]):
        bullets = find_lines_by_terms(ctx, [
            "স্নাতকোত্তর", "স্নাতক", "উচ্চ মাধ্যমিক", "মাধ্যমিক", "গ্রেড-১৩ এবং গ্রেড-১৪", "গ্রেড-১৫"
        ], 10)
        if bullets:
            return format_simple_answer(
                "নীতিমালায় গ্রেডভিত্তিক শিক্ষাগত যোগ্যতা এভাবে বলা হয়েছে।",
                bullets,
                citations,
            )

    return None


TOPIC_HANDLERS = {
    "grade_structure": answer_grade_structure,
    "recruitment_policy": answer_recruitment_policy,
    "promotion_procedure": answer_promotion_procedure,
    "transfer_policy": answer_transfer_policy,
    "disciplinary_policy_full": answer_disciplinary_policy_full,
    "performance_evaluation": answer_performance_evaluation,
    "educational_qualification": answer_educational_qualification,
    "service_rules_6_2": answer_service_rules_gratuity,
    "service_rules_6_11": answer_comm_facilities,
    "project_staff_7_2": answer_project_staff_recruitment,
}


def deterministic_answer_for_topic(
    question_raw: str,
    detected_topic: Optional[Dict[str, str]],
    ctx: str,
    citations: List[Dict[str, Any]],
) -> Optional[str]:
    if not detected_topic or not ctx.strip():
        return None

    if clean(detected_topic.get("intent_type")) == "compare_topics":
        return answer_compare_general_vs_project_recruitment(question_raw, ctx, citations)

    manual_key = clean(detected_topic.get("manual_key"))
    topic_id = clean(detected_topic.get("topic_id"))

    handler = None

    if manual_key:
        handler = TOPIC_HANDLERS.get(manual_key)

    if not handler and topic_id:
        handler = TOPIC_HANDLERS.get(topic_id)

    if not handler:
        return None

    try:
        return handler(question_raw, ctx, citations)
    except Exception:
        return None


def format_policy_answer(
    question_raw: str,
    ctx: str,
    citations: List[Dict[str, Any]],
    detected_topic: Optional[Dict[str, Any]],
    is_bn_func,
) -> str:
    broad_answer = answer_end_of_service_benefits(question_raw, ctx, citations)
    if broad_answer:
        return broad_answer

    deterministic = deterministic_answer_for_topic(question_raw, detected_topic, ctx, citations)
    if deterministic:
        return deterministic

    lines = [ln.strip() for ln in ctx.splitlines() if ln.strip()]
    return format_simple_answer(
        "নীতিমালার প্রাসঙ্গিক উদ্ধৃতি নিচে দেওয়া হলো।" if is_bn_func(question_raw) else "The relevant policy excerpt is shown below.",
        lines[:20],
        citations,
    )


def suggest_questions(topic: Optional[Dict[str, str]], question: str) -> List[str]:
    topic_id = clean((topic or {}).get("topic_id"))
    topic_bn = clean((topic or {}).get("topic_bn"))
    intent_type = clean((topic or {}).get("intent_type"))

    if intent_type == "compare_topics":
        return [
            "সাধারণ কর্মী নিয়োগে বয়সসীমা কী?",
            "প্রকল্প কর্মী নিয়োগে বয়সসীমা কী?",
            "সাধারণ ও প্রকল্প কর্মী নিয়োগে নিয়োগপত্র কারা ইস্যু করেন?",
        ]

    if topic_id == "service_rules_6_11" or topic_bn == "টেলিফোন , ইন্টারনেট ও ই-মেইল সুবিধা":
        return [
            "কর্মীদের টেলিফোন সুবিধা কী?",
            "ইন্টারনেট সুবিধা কীভাবে পাওয়া যাবে?",
            "ইমেইল সুবিধা কারা পাবেন?",
        ]

    if topic_id == "service_rules_6_2" or topic_bn == "গ্র্যাচুয়িটি":
        return [
            "গ্র্যাচুয়িটি ৫ বছর পূর্ণ না হলে পাওয়া যাবে কি?",
            "১২ বছর ও ২০ বছর চাকুরীতে গ্র্যাচুয়িটি কীভাবে হিসাব হবে?",
            "ভগ্ন মাস থাকলে গ্র্যাচুয়িটি কীভাবে গণনা হবে?",
        ]

    if topic_id == "project_staff_7_2" or topic_bn == "প্রকল্প কর্মীদের নিয়োগের নিয়মাবলী":
        return [
            "প্রকল্প কর্মী নিয়োগ কমিটি কীভাবে গঠিত হবে?",
            "প্রকল্প কর্মীর বয়সসীমা কত?",
            "প্রকল্প কর্মীর নিয়োগপত্র কে ইস্যু করবে?",
        ]

    if topic_id.startswith("policy_1_3") or topic_bn == "গ্রেড কাঠামো":
        return [
            "গ্রেড-১ এ কোন পদ আছে?",
            "গ্রেড-১০ এ কোন পদ আছে?",
            "গ্রেড-১৬ এ কোন পদ আছে?",
        ]

    if topic_id.startswith("policy_2_3") or topic_bn == "পদোন্নতি পদ্ধতি":
        return [
            "কোন গ্রেডে লিখিত পরীক্ষা বাধ্যতামূলক?",
            "মৌখিক পরীক্ষায় নম্বর কীভাবে গণনা করা হবে?",
            "গ্রেড-১৫ থেকে গ্রেড-১৪ পদোন্নতিতে পরীক্ষা লাগবে কি?",
        ]

    if topic_id.startswith("policy_1_5") or topic_bn == "কর্মী নিয়োগের নীতিমালা":
        return [
            "গ্রেড-১১ থেকে গ্রেড-১৬ পর্যন্ত নিয়োগের বয়সসীমা কী?",
            "পদত্যাগের পর পুনরায় নিয়োগের শর্ত কী?",
            "নিয়োগের আগে মূল সনদপত্র কি জমা বা দেখাতে হবে?",
        ]

    if topic_id.startswith("policy_1_8") or topic_bn == "বদলী নীতিমালা":
        return [
            "বদলির ক্ষেত্রে কর্তৃপক্ষের ক্ষমতা কী?",
            "কর্মীকে কোথায় কোথায় বদলি করা যেতে পারে?",
            "বদলির আদেশ অমান্য করলে কী হবে?",
        ]

    if topic_id.startswith("policy_3_2_3_6_13") or topic_bn == "দন্ডযোগ্য অপরাধ, দন্ডের প্রকার ও শাস্তিমূলক ব্যবস্থা":
        return [
            "কারণ দর্শানো নোটিশে কত দিনের মধ্যে জবাব দিতে হবে?",
            "ধর্ষণ বা ধর্ষণের চেষ্টার ক্ষেত্রে কী শাস্তি হবে?",
            "অর্থ তসরুফ বা তথ্য গোপনের ক্ষেত্রে কী ব্যবস্থা আছে?",
        ]

    if topic_id.startswith("policy_2_4") or topic_bn == "কর্মীর দক্ষতা মূল্যায়ন পদ্ধতি":
        return [
            "annual performance appraisal কবে করতে হবে?",
            "গ্রেড-১ এর মূল্যায়ন কে করবে?",
            "গর্ভধারণকে মূল্যায়নে কীভাবে দেখা হবে?",
        ]

    if topic_id.startswith("policy_1_4") or topic_bn == "কর্মীদের পদ বিন্যাস অনুযায়ী শিক্ষাগত যোগ্যতা":
        return [
            "গ্রেড-১৫ পদের শিক্ষাগত যোগ্যতা কী?",
            "গ্রেড-১৪ পদের শিক্ষাগত যোগ্যতা কী?",
            "গ্রেড-১৩ এবং গ্রেড-১৪ এর শিক্ষাগত যোগ্যতা কী?",
        ]

    return [
        "এই বিষয়ে বিস্তারিত কী?",
        "এই নীতির মূল শর্তগুলো কী?",
        "এখানে কোনো ব্যতিক্রম আছে কি?",
    ]
