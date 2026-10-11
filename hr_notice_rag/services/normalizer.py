from hr_notice_rag.utils.regex_patterns import COPY_TO_MARKERS, SIGNATURE_MARKERS
from hr_notice_rag.utils.text_cleaning import clean_line, normalize_whitespace


HEADER_CONTAINS = [
    "www.",
    "@",
    "phone",
    "email",
    "website",
    "fax",
]


def normalize_blocks(blocks):
    cleaned = []
    for b in blocks:
        text = clean_line(b.get("text", ""))
        if not text:
            continue
        cleaned.append({**b, "text": text})

    body_blocks = []
    excluded_blocks = []

    cut_mode = None

    for idx, b in enumerate(cleaned):
        text = b["text"]
        low = text.lower()

        if idx < 5 and any(k in low for k in HEADER_CONTAINS):
            excluded_blocks.append({**b, "reason": "header_noise"})
            continue

        if any(low.startswith(m) for m in COPY_TO_MARKERS):
            cut_mode = "copy_to"
            excluded_blocks.append({**b, "reason": "copy_to_start"})
            continue

        if any(low.startswith(m) for m in SIGNATURE_MARKERS):
            cut_mode = "signature"
            excluded_blocks.append({**b, "reason": "signature_start"})
            continue

        if cut_mode:
            excluded_blocks.append({**b, "reason": cut_mode})
            continue

        body_blocks.append(b)

    normalized_text = normalize_whitespace("\n".join([b["text"] for b in body_blocks]))
    excluded_text = normalize_whitespace("\n".join([b["text"] for b in excluded_blocks]))

    return {
        "body_blocks": body_blocks,
        "excluded_blocks": excluded_blocks,
        "normalized_text": normalized_text,
        "excluded_text": excluded_text,
    }
