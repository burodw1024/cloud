def chunk_text(text, chunk_size=900, overlap=120):
    text = " ".join((text or "").split())
    if not text:
        return []

    chunks = []
    start = 0

    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start = max(end - overlap, end)

    return chunks
