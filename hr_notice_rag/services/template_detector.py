def detect_template_type(blocks):
    sample = " ".join([b["text"] for b in blocks[:12]]).lower()

    if "office order" in sample:
        return "office_order"
    if "subject" in sample and "memo" in sample:
        return "memo_notice"
    if "circular" in sample:
        return "circular"
    if "notice" in sample:
        return "standard_notice"
    return "generic_notice"
