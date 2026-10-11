import re


def normalize_whitespace(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean_line(text: str) -> str:
    text = text.replace("\xa0", " ").strip()
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def is_meaningful_line(text: str) -> bool:
    return bool(text and len(text.strip()) > 1)
