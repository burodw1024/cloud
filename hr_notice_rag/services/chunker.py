def chunk_notice_text(title, notice_no, publish_date, normalized_text, max_words=700, overlap_words=100):
    words = normalized_text.split()
    if not words:
        return []

    header_lines = []
    if title:
        header_lines.append(f"Title: {title}")
    if notice_no:
        header_lines.append(f"Notice No: {notice_no}")
    if publish_date:
        header_lines.append(f"Date: {publish_date.isoformat()}")
    header = "\n".join(header_lines).strip()

    chunks = []
    start = 0
    idx = 0

    while start < len(words):
        end = min(start + max_words, len(words))
        body = " ".join(words[start:end]).strip()
        text = f"{header}\n\n{body}" if header else body
        chunks.append({
            "chunk_index": idx,
            "section_name": "",
            "chunk_text": text.strip(),
        })
        if end >= len(words):
            break
        start = max(0, end - overlap_words)
        idx += 1

    return chunks
