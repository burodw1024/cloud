from __future__ import annotations

import re
from typing import Optional

from .schema import DateRange, Entities


def _safe_int(v: str, default: int, lo: int, hi: int) -> int:
    try:
        x = int(v)
    except Exception:
        x = default
    return max(lo, min(hi, x))


def extract_limit(q_norm: str, default_limit: int, max_limit: int) -> int:
    s = (q_norm or "").lower()
    m = re.search(r"\btop\s+(\d{1,5})\b", s)
    if m:
        return _safe_int(m.group(1), default_limit, 1, max_limit)
    m2 = re.search(r"\blimit\s+(\d{1,5})\b", s)
    if m2:
        return _safe_int(m2.group(1), default_limit, 1, max_limit)
    return default_limit


def extract_customerid(q_norm: str) -> Optional[str]:
    s = (q_norm or "").strip()
    # prefer explicit customerid patterns
    pats = [
        r"\bcustomerid\s*[:=]?\s*([0-9]{3,20})\b",
    ]
    last = None
    for p in pats:
        for m in re.finditer(p, s, flags=re.IGNORECASE):
            last = m.group(1)

    # fallback: if only one number present
    if not last:
        nums = re.findall(r"\b[0-9]{3,20}\b", s)
        if len(nums) == 1:
            last = nums[0]
    return last


def extract_branch(q_norm: str) -> Optional[str]:
    m = re.search(r"\bbranch\b\s*[:=\-]?\s*([A-Za-z0-9_ .()]+)", q_norm, flags=re.IGNORECASE)
    if not m:
        return None
    val = (m.group(1) or "").strip()
    val = re.split(r"\b(top|limit|customerid|district)\b", val, flags=re.IGNORECASE)[0].strip()
    return val[:80] if val else None


def extract_district(q_norm: str) -> Optional[str]:
    m = re.search(r"\bdistrict\b\s*[:=\-]?\s*([A-Za-z0-9_ .()]+)", q_norm, flags=re.IGNORECASE)
    if not m:
        return None
    val = (m.group(1) or "").strip()
    val = re.split(r"\b(top|limit|customerid|branch)\b", val, flags=re.IGNORECASE)[0].strip()
    return val[:80] if val else None


def extract_status(q_norm: str) -> Optional[str]:
    s = (q_norm or "").lower()
    if re.search(r"\bactive\b", s):
        return "active"
    if re.search(r"\b(inactive|closed|blocked)\b", s):
        return "inactive"
    return None


def extract_date_range(q_norm: str) -> DateRange:
    s = (q_norm or "").lower().strip()

    if "today" in s:
        return DateRange(preset="today")
    if "this month" in s or "current month" in s:
        return DateRange(preset="this_month")

    m = re.search(r"\blast\s+(\d{1,3})\s+days\b", s)
    if m:
        d = _safe_int(m.group(1), 7, 1, 365)
        return DateRange(preset=f"last_{d}_days")

    m3 = re.search(r"\bfrom\s+([0-9]{4}-[0-9]{2}-[0-9]{2})\s+to\s+([0-9]{4}-[0-9]{2}-[0-9]{2})\b", s)
    if m3:
        return DateRange(date_from=m3.group(1), date_to=m3.group(2))

    m4 = re.search(r"\bbetween\s+([0-9]{4}-[0-9]{2}-[0-9]{2})\s+and\s+([0-9]{4}-[0-9]{2}-[0-9]{2})\b", s)
    if m4:
        return DateRange(date_from=m4.group(1), date_to=m4.group(2))

    return DateRange()


def extract_entities(q_norm: str, default_limit: int, max_limit: int) -> Entities:
    e = Entities()
    e.limit = extract_limit(q_norm, default_limit, max_limit)
    e.customerid = extract_customerid(q_norm)
    e.branch = extract_branch(q_norm)
    e.district = extract_district(q_norm)
    e.status = extract_status(q_norm)
    e.date = extract_date_range(q_norm)
    return e
