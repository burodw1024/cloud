from __future__ import annotations

import re
from typing import List

from buro_first.ai_intent.engine import IntentRule


def _re(p: str):
    return re.compile(p, re.IGNORECASE)


def cashflow_rules() -> List[IntentRule]:
    return [
        IntentRule(
            intent="count_customers",
            match=lambda q: bool(_re(r"\b(how many|count|total|number of)\s+customers?\b").search(q))
            or ("মোট গ্রাহক" in q),
            required=[],
            base_confidence=0.92,
        ),
        IntentRule(
            intent="count_customer_details",
            match=lambda q: ("details" in q.lower() and bool(_re(r"\b(how many|count|total|number of)\b").search(q)))
            or ("ডিটেইল" in q and ("কত" in q or "মোট" in q or "সংখ্যা" in q)),
            required=[],
            base_confidence=0.90,
        ),
        IntentRule(
            intent="last_details_date",
            match=lambda q: (("details" in q.lower()) and bool(_re(r"\b(last|latest|most recent|recent)\b").search(q)))
            or ("ডিটেইল" in q and ("সর্বশেষ" in q or "লেটেস্ট" in q or "শেষ" in q)),
            required=[],
            base_confidence=0.90,
        ),
        IntentRule(
            intent="customer_info_by_id",
            match=lambda q: ("customer info" in q.lower()) or ("গ্রাহক তথ্য" in q) or ("কাস্টমার ইনফো" in q),
            required=["customerid"],
            base_confidence=0.88,
        ),
        IntentRule(
            intent="customer_details_by_id",
            match=lambda q: ("customer details" in q.lower()) or ("ডিটেইল" in q),
            required=["customerid"],
            base_confidence=0.85,
        ),
        IntentRule(
            intent="latest_customer_details_by_id",
            match=lambda q: (("customer details" in q.lower() or "ডিটেইল" in q) and ("latest" in q.lower() or "last" in q.lower()))
            or ("ডিটেইল" in q and ("সর্বশেষ" in q or "লেটেস্ট" in q or "শেষ" in q)),
            required=["customerid"],
            base_confidence=0.88,
        ),
        IntentRule(
            intent="group_customers_by_branch",
            match=lambda q: ("branch wise" in q.lower() and "customer" in q.lower())
            or ("ব্রাঞ্চ" in q and ("ভিত্তিক" in q or "ওয়াইজ" in q) and ("গ্রাহক" in q or "কাস্টমার" in q)),
            required=[],
            base_confidence=0.86,
        ),
        IntentRule(
            intent="list_customers",
            match=lambda q: (("list" in q.lower() or "show" in q.lower()) and "customer" in q.lower() and "details" not in q.lower())
            or (("তালিকা" in q or "লিস্ট" in q) and ("গ্রাহক" in q or "কাস্টমার" in q) and ("ডিটেইল" not in q)),
            required=[],
            base_confidence=0.82,
        ),
        IntentRule(
            intent="list_customer_details",
            match=lambda q: (("list" in q.lower() or "show" in q.lower()) and "details" in q.lower())
            or (("তালিকা" in q or "লিস্ট" in q) and ("ডিটেইল" in q or "ডিটেল" in q)),
            required=[],
            base_confidence=0.82,
        ),
    ]
