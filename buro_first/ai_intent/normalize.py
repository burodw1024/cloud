from __future__ import annotations

import re
from typing import Tuple


def normalize_text(q: str) -> Tuple[str, str]:
    raw = (q or "").strip()
    s = raw

    # unify common tokens
    repl = [
        (r"\bcust\s*id\b", "customerid"),
        (r"\bcustomer\s*id\b", "customerid"),
        (r"\bcustomer[_\s-]*id\b", "customerid"),
        (r"\bclient\s*id\b", "customerid"),
        (r"\bbranch\s*wise\b", "branch wise"),
        (r"\bdistrict\s*wise\b", "district wise"),
    ]
    for pat, to in repl:
        s = re.sub(pat, to, s, flags=re.IGNORECASE)

    # Bangla variants (lightweight)
    s = s.replace("কাস্টমার আইডি", "customerid")
    s = s.replace("গ্রাহক আইডি", "customerid")
    s = s.replace("ব্রাঞ্চ", "branch")
    s = s.replace("জেলা", "district")
    s = s.replace("সর্বশেষ", "latest")
    s = s.replace("লেটেস্ট", "latest")
    s = s.replace("শেষ", "last")
    s = s.replace("এই মাস", "this month")
    s = s.replace("চলতি মাস", "this month")
    s = s.replace("আজ", "today")

    # collapse spaces
    s = re.sub(r"\s+", " ", s).strip()
    return raw, s
