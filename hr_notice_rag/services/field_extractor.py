import re
from datetime import datetime
from typing import Optional


MONTH_NAME_FORMATS = [
    "%d %B %Y",
    "%d %b %Y",
]

NUMERIC_DATE_FORMATS = [
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%Y/%m/%d",
    "%Y-%m-%d",
    "%Y.%m.%d",
    "%d/%m/%y",
    "%d-%m-%y",
    "%d.%m.%y",
]


def _try_parse_date(value: str) -> Optional[datetime.date]:
    value = (value or "").strip()
    for fmt in MONTH_NAME_FORMATS + NUMERIC_DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except Exception:
            continue
    return None


def extract_fields(raw_text: str, normalized_text: str = "") -> dict:
    raw_text = raw_text or ""
    normalized_text = normalized_text or ""

    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]

    title = ""
    notice_no = ""
    publish_date = None
    department = ""

    # 1) Title: prefer Subject line
    for line in lines[:20]:
        m = re.match(r"(?i)^\s*subject\s*[:\-]\s*(.+)$", line)
        if m:
            title = m.group(1).strip()
            break

    # 2) Fallback title
    if not title:
        for line in lines[:10]:
            low = line.lower()
            if all(
                x not in low
                for x in [
                    "notice no",
                    "memo no",
                    "effective date",
                    "date |",
                    "department |",
                    "www.",
                    "@",
                ]
            ):
                if 8 < len(line) < 220:
                    title = line
                    break

    # 3) Notice No
    notice_patterns = [
        r"(?i)\bnotice\s+no\s*\|\s*([A-Za-z0-9/._-]+)",
        r"(?i)\bnotice\s+no\s*[:\-]\s*([A-Za-z0-9/._-]+)",
        r"(?i)\bmemo\s+no\s*\|\s*([A-Za-z0-9/._-]+)",
        r"(?i)\bmemo\s+no\s*[:\-]\s*([A-Za-z0-9/._-]+)",
        r"(?i)\bref(?:erence)?\s+no\s*[:\-]\s*([A-Za-z0-9/._-]+)",
    ]
    for pat in notice_patterns:
        m = re.search(pat, raw_text)
        if m:
            notice_no = m.group(1).strip()
            break

    # 4) Publish Date: prefer explicit Date field, avoid Effective Date
    publish_patterns = [
        r"(?i)(?<!effective\s)\bdate\s*\|\s*([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4})",
        r"(?i)(?<!effective\s)\bdate\s*[:\-]\s*([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4})",
    ]
    for pat in publish_patterns:
        m = re.search(pat, raw_text)
        if m:
            dt = _try_parse_date(m.group(1))
            if dt:
                publish_date = dt
                break

    # 5) Fallback generic date
    if not publish_date:
        generic_dates = re.findall(r"\b([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4})\b", raw_text)
        for value in generic_dates:
            dt = _try_parse_date(value)
            if dt:
                publish_date = dt
                break

    # 6) Department
    combined = f"{raw_text}\n{normalized_text}"
    if re.search(r"(?i)\bhuman resources\b", combined):
        department = "HR"
    elif re.search(r"(?i)\bhr\b", combined):
        department = "HR"

    return {
        "title": title[:500],
        "notice_no": notice_no[:200],
        "publish_date": publish_date,
        "department": department[:200],
    }
