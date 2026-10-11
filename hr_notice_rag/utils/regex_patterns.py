NOTICE_NO_PATTERNS = [
    r'(?i)\b(?:memo|memorandum|notice|circular|office order|ref(?:erence)?)[\s\-_:]*no\.?[\s:]*([A-Za-z0-9\/\-\._]+)',
    r'(?i)\bno\.?[\s:]*([A-Za-z0-9\/\-\._]{3,})',
]

DATE_PATTERNS = [
    r'\b(\d{1,2}[\/\-.]\d{1,2}[\/\-.]\d{2,4})\b',
    r'\b(\d{4}[\/\-.]\d{1,2}[\/\-.]\d{1,2})\b',
]

SUBJECT_PATTERNS = [
    r'(?i)^\s*subject\s*[:\-]\s*(.+)$',
    r'(?i)^\s*sub\s*[:\-]\s*(.+)$',
]

COPY_TO_MARKERS = [
    "copy to",
    "cc:",
    "for information and necessary action",
    "distribution",
]

SIGNATURE_MARKERS = [
    "authorized signature",
    "by order",
    "sincerely",
    "regards",
]
