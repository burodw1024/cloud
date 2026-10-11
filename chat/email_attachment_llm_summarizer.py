import os
import re
import json
import time
import html
import hashlib
import imaplib
import email
from pathlib import Path
from datetime import datetime
from email.header import decode_header
from typing import Optional, List, Dict, Any, Tuple

import requests
from pypdf import PdfReader


# ============================================================
# ENV HELPERS
# ============================================================

def env_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "y")


def env_int(name: str, default: str) -> int:
    value = os.getenv(name, default).strip()
    try:
        return int(value)
    except ValueError:
        return int(default)


def env_float(name: str, default: str) -> float:
    value = os.getenv(name, default).strip()
    try:
        return float(value)
    except ValueError:
        return float(default)


# ============================================================
# EMAIL / IMAP CONFIG - GMAIL DEFAULT
# ============================================================

IMAP_HOST = os.getenv("IMAP_HOST", "imap.gmail.com")
IMAP_PORT = env_int("IMAP_PORT", "993")

EMAIL_USER = os.getenv("EMAIL_USER", "burodw1024@gmail.com")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")

MAIL_FOLDER = os.getenv("MAIL_FOLDER", "INBOX")


# ============================================================
# FILTER CONFIG
# ============================================================

ONLY_UNREAD = env_bool("ONLY_UNREAD", "false")

SUBJECT_KEYWORD = os.getenv("SUBJECT_KEYWORD", "").strip().lower()
SENDER_FILTER = os.getenv("SENDER_FILTER", "").strip().lower()
ATTACHMENT_NAME_KEYWORD = os.getenv("ATTACHMENT_NAME_KEYWORD", "").strip().lower()

MAX_EMAILS_TO_CHECK = env_int("MAX_EMAILS_TO_CHECK", "30")
MAX_ATTACHMENTS_PER_RUN = env_int("MAX_ATTACHMENTS_PER_RUN", "10")

MARK_EMAIL_AS_READ_AFTER_PROCESS = env_bool(
    "MARK_EMAIL_AS_READ_AFTER_PROCESS",
    "false"
)


# ============================================================
# OLLAMA CONFIG
# ============================================================

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://192.168.1.144:31134").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "aya-23-8b:q5")

OLLAMA_CONNECT_TIMEOUT_SECONDS = env_int("OLLAMA_CONNECT_TIMEOUT_SECONDS", "30")
OLLAMA_READ_TIMEOUT_SECONDS = env_int("OLLAMA_READ_TIMEOUT_SECONDS", "900")

MAX_TEXT_CHARS_FOR_LLM = env_int("MAX_TEXT_CHARS_FOR_LLM", "3500")
OLLAMA_NUM_CTX = env_int("OLLAMA_NUM_CTX", "4096")
OLLAMA_NUM_PREDICT = env_int("OLLAMA_NUM_PREDICT", "800")
OLLAMA_TEMPERATURE = env_float("OLLAMA_TEMPERATURE", "0.1")


# ============================================================
# GOTENBERG CONFIG
# ============================================================
# Your tested NodePort URL:
#   curl http://192.168.1.136:31164/health
#
# If the app runs inside Kubernetes, you can instead use:
#   http://gotenberg.datalake.svc.cluster.local:3000

GOTENBERG_URL = os.getenv("GOTENBERG_URL", "http://192.168.1.136:31164").rstrip("/")
GOTENBERG_TIMEOUT_SECONDS = env_int("GOTENBERG_TIMEOUT_SECONDS", "300")
CHECK_GOTENBERG_ON_START = env_bool("CHECK_GOTENBERG_ON_START", "true")

# If true, unknown non-dangerous extensions will be attempted through Gotenberg.
# Unsupported files will fail gracefully and be counted in failed_attachments.
PROCESS_UNKNOWN_ATTACHMENTS_WITH_GOTENBERG = env_bool(
    "PROCESS_UNKNOWN_ATTACHMENTS_WITH_GOTENBERG",
    "true",
)


# ============================================================
# FILE CONFIG
# ============================================================

BASE_DIR = Path(os.getenv("EMAIL_PROCESS_BASE_DIR", "./email_process_data"))
DOWNLOAD_DIR = BASE_DIR / "downloaded_attachments"
CONVERTED_PDF_DIR = BASE_DIR / "converted_pdfs"
SUMMARY_DIR = BASE_DIR / "summaries"
JSON_SUMMARY_DIR = BASE_DIR / "summaries_json"

PROCESSED_DB_FILE = BASE_DIR / "processed_attachments.json"
SUMMARY_INDEX_FILE = BASE_DIR / "summary_index.json"

for directory in [BASE_DIR, DOWNLOAD_DIR, CONVERTED_PDF_DIR, SUMMARY_DIR, JSON_SUMMARY_DIR]:
    directory.mkdir(parents=True, exist_ok=True)


# Files that can be read directly without conversion.
DIRECT_TEXT_EXTENSIONS = {
    ".txt", ".md", ".csv", ".json", ".xml", ".log",
}

PDF_EXTENSIONS = {
    ".pdf",
}

# Common Office / LibreOffice formats that Gotenberg can usually convert to PDF.
GOTENBERG_OFFICE_EXTENSIONS = {
    # Microsoft Word
    ".doc", ".docx", ".docm", ".dot", ".dotx", ".dotm",

    # Microsoft Excel
    ".xls", ".xlsx", ".xlsm", ".xlt", ".xltx", ".xltm",

    # Microsoft PowerPoint
    ".ppt", ".pptx", ".pptm", ".pot", ".potx", ".potm", ".pps", ".ppsx", ".ppsm",

    # OpenDocument / LibreOffice
    ".odt", ".ott", ".ods", ".ots", ".odp", ".otp", ".odg",

    # Other common document formats
    ".rtf", ".html", ".htm",
}

# Do not attempt these through Gotenberg.
# They are not useful for text summary and may be unsafe/unnecessary to process.
BLOCKED_EXTENSIONS = {
    ".exe", ".dll", ".bat", ".cmd", ".com", ".msi", ".sh", ".ps1",
    ".bin", ".iso", ".img", ".apk",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz",
    ".mp3", ".mp4", ".avi", ".mkv", ".mov", ".wav",
}

ALLOWED_EXTENSIONS = (
    DIRECT_TEXT_EXTENSIONS
    | PDF_EXTENSIONS
    | GOTENBERG_OFFICE_EXTENSIONS
)


# ============================================================
# CHARITY / AID VERIFICATION PROMPT
# ============================================================

CHARITY_AID_SUMMARY_PROMPT_TEMPLATE = """
You are a document summarizer for charity/aid verification.

Rules:
1. Use only the information found in the document.
2. Do not invent any fact.
3. If information is missing, write "উল্লেখ নেই".
4. Do not add assumptions about loan rejection, credit history, milk selling, meat selling, or income plan unless explicitly written.
5. Output must be in clean Bangla only.
6. Avoid repeated paragraphs.
7. Correct spelling naturally, but do not change meaning.
8. Structure the answer under these headings:
   - আবেদনকারীর পরিচিতি
   - পারিবারিক অবস্থা
   - আর্থিক অবস্থা
   - অভিজ্ঞতা ও সক্ষমতা
   - সহায়তার উদ্দেশ্য
   - ঝুঁকি/যাচাই প্রয়োজন
   - সংক্ষিপ্ত সুপারিশ

ইমেইল বিষয়: {email_subject}
প্রেরক: {email_sender}
ডকুমেন্টের নাম: {filename}

ডকুমেন্টের লেখা:
{text}
""".strip()


# ============================================================
# UTILITY FUNCTIONS
# ============================================================

def now_string() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def now_file_string() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def log(message: str) -> None:
    print(f"[{now_string()}] {message}", flush=True)


def safe_filename(name: str) -> str:
    name = name.replace("/", "_").replace("\\", "_")
    name = re.sub(r"[^A-Za-z0-9._\-\u0980-\u09FF ]+", "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name or f"file_{int(time.time())}"


def decode_mime_words(value: Optional[str]) -> str:
    if not value:
        return ""

    decoded_parts = decode_header(value)
    result = ""

    for part, encoding in decoded_parts:
        if isinstance(part, bytes):
            result += part.decode(encoding or "utf-8", errors="ignore")
        else:
            result += part

    return result


def file_sha256(file_path: Path) -> str:
    h = hashlib.sha256()

    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)

    return h.hexdigest()


def load_json_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return default

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json_file(path: Path, data: Any) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def load_processed_db() -> Dict[str, Any]:
    return load_json_file(PROCESSED_DB_FILE, {})


def save_processed_db(db: Dict[str, Any]) -> None:
    write_json_file(PROCESSED_DB_FILE, db)


def load_summary_index() -> List[Dict[str, Any]]:
    data = load_json_file(SUMMARY_INDEX_FILE, [])
    if isinstance(data, list):
        return data
    return []


def save_summary_index(index: List[Dict[str, Any]]) -> None:
    # keep latest 200 summaries only
    index = sorted(index, key=lambda x: x.get("processed_time", ""), reverse=True)
    index = index[:200]
    write_json_file(SUMMARY_INDEX_FILE, index)


def clean_summary_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in text.split("\n")]

    cleaned_lines = []
    previous = None

    for line in lines:
        if not line:
            if cleaned_lines and cleaned_lines[-1] != "":
                cleaned_lines.append("")
            continue

        # avoid repeated paragraphs/lines
        if line == previous:
            continue

        cleaned_lines.append(line)
        previous = line

    return "\n".join(cleaned_lines).strip()


def should_attempt_attachment(filename: str) -> bool:
    ext = Path(filename).suffix.lower()

    if not ext:
        log(f"Skipping attachment without extension: {filename}")
        return False

    if ext in BLOCKED_EXTENSIONS:
        log(f"Skipping blocked attachment type: {filename}")
        return False

    if ext in ALLOWED_EXTENSIONS:
        return True

    if PROCESS_UNKNOWN_ATTACHMENTS_WITH_GOTENBERG:
        log(f"Unknown extension will be attempted through Gotenberg: {filename}")
        return True

    log(f"Skipping unsupported attachment: {filename}")
    return False


# ============================================================
# SUMMARY HTML FORMATTER FOR CHAT APP
# ============================================================

SUMMARY_HEADINGS = [
    "আবেদনকারীর পরিচিতি",
    "পারিবারিক অবস্থা",
    "আর্থিক অবস্থা",
    "অভিজ্ঞতা ও সক্ষমতা",
    "সহায়তার উদ্দেশ্য",
    "ঝুঁকি/যাচাই প্রয়োজন",
    "সংক্ষিপ্ত সুপারিশ",
]


def split_summary_sections(summary: str) -> Dict[str, str]:
    sections: Dict[str, str] = {}
    current_heading = None
    buffer: List[str] = []

    def flush() -> None:
        nonlocal buffer, current_heading
        if current_heading:
            sections[current_heading] = "\n".join(buffer).strip()
        buffer = []

    for raw_line in summary.splitlines():
        line = raw_line.strip()
        normalized = line.replace(":", "").strip()

        matched_heading = None
        for heading in SUMMARY_HEADINGS:
            if normalized == heading or normalized.endswith(heading):
                matched_heading = heading
                break

        if matched_heading:
            flush()
            current_heading = matched_heading
            continue

        if current_heading:
            buffer.append(line)

    flush()

    # fallback: if model did not follow heading format
    if not sections:
        sections["সংক্ষিপ্ত সুপারিশ"] = summary.strip()

    return sections


def summary_to_html_card(record: Dict[str, Any]) -> str:
    summary = record.get("summary", "")
    sections = split_summary_sections(summary)

    file_name = html.escape(record.get("attachment_file_name", "উল্লেখ নেই"))
    converted_file = html.escape(record.get("converted_pdf_file_name", "") or "")
    subject = html.escape(record.get("email_subject", "উল্লেখ নেই"))
    sender = html.escape(record.get("email_sender", "উল্লেখ নেই"))
    processed_time = html.escape(record.get("processed_time", "উল্লেখ নেই"))
    model = html.escape(record.get("ollama_model", "উল্লেখ নেই"))
    conversion_method = html.escape(record.get("conversion_method", "উল্লেখ নেই"))

    converted_line = ""
    if converted_file:
        converted_line = f"<b>কনভার্টেড PDF:</b> {converted_file}<br>"

    section_html_parts = []

    for heading in SUMMARY_HEADINGS:
        value = sections.get(heading, "").strip() or "উল্লেখ নেই"
        value = html.escape(value)
        value = value.replace("\n", "<br>")

        section_html_parts.append(
            f"""
            <div style="border:1px solid #e5e7eb;border-radius:10px;padding:12px;margin:10px 0;background:#ffffff;">
              <div style="font-weight:700;color:#1f2937;margin-bottom:6px;">{html.escape(heading)}</div>
              <div style="color:#374151;line-height:1.7;">{value}</div>
            </div>
            """
        )

    return f"""
    <div style="font-family:Arial, sans-serif;background:#f9fafb;border:1px solid #d1d5db;border-radius:14px;padding:16px;margin:10px 0;">
      <div style="font-size:18px;font-weight:700;color:#111827;margin-bottom:10px;">
        ইমেইল সংযুক্তির সহায়তা যাচাই সারাংশ
      </div>

      <div style="background:#eef2ff;border-radius:10px;padding:10px;margin-bottom:12px;color:#1f2937;line-height:1.6;">
        <b>ডকুমেন্ট:</b> {file_name}<br>
        {converted_line}
        <b>প্রসেস পদ্ধতি:</b> {conversion_method}<br>
        <b>ইমেইল বিষয়:</b> {subject}<br>
        <b>প্রেরক:</b> {sender}<br>
        <b>প্রসেস সময়:</b> {processed_time}<br>
        <b>মডেল:</b> {model}
      </div>

      {''.join(section_html_parts)}
    </div>
    """.strip()


def summary_to_markdown_card(record: Dict[str, Any]) -> str:
    summary = record.get("summary", "")
    sections = split_summary_sections(summary)

    lines = [
        "## ইমেইল সংযুক্তির সহায়তা যাচাই সারাংশ",
        "",
        f"**ডকুমেন্ট:** {record.get('attachment_file_name', 'উল্লেখ নেই')}",
    ]

    if record.get("converted_pdf_file_name"):
        lines.append(f"**কনভার্টেড PDF:** {record.get('converted_pdf_file_name')}")

    lines.extend([
        f"**প্রসেস পদ্ধতি:** {record.get('conversion_method', 'উল্লেখ নেই')}",
        f"**ইমেইল বিষয়:** {record.get('email_subject', 'উল্লেখ নেই')}",
        f"**প্রেরক:** {record.get('email_sender', 'উল্লেখ নেই')}",
        f"**প্রসেস সময়:** {record.get('processed_time', 'উল্লেখ নেই')}",
        "",
    ])

    for heading in SUMMARY_HEADINGS:
        lines.append(f"### {heading}")
        lines.append(sections.get(heading, "").strip() or "উল্লেখ নেই")
        lines.append("")

    return "\n".join(lines).strip()


# ============================================================
# OLLAMA FUNCTIONS
# ============================================================

def check_ollama_available() -> None:
    url = f"{OLLAMA_URL}/api/tags"

    try:
        response = requests.get(url, timeout=20)
    except Exception as e:
        raise RuntimeError(
            f"Could not connect to Ollama at {OLLAMA_URL}. Error: {e}"
        )

    if response.status_code >= 400:
        raise RuntimeError(
            f"Ollama check failed: {response.status_code} {response.text}"
        )

    log(f"Ollama reachable: {OLLAMA_URL}")


def summarize_with_ollama(
    text: str,
    filename: str,
    email_subject: str,
    email_sender: str,
) -> str:
    if not text.strip():
        return "উল্লেখ নেই"

    trimmed_text = text[:MAX_TEXT_CHARS_FOR_LLM]

    prompt = CHARITY_AID_SUMMARY_PROMPT_TEMPLATE.format(
        email_subject=email_subject or "উল্লেখ নেই",
        email_sender=email_sender or "উল্লেখ নেই",
        filename=filename or "উল্লেখ নেই",
        text=trimmed_text,
    )

    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": OLLAMA_TEMPERATURE,
            "num_ctx": OLLAMA_NUM_CTX,
            "num_predict": OLLAMA_NUM_PREDICT,
        }
    }

    url = f"{OLLAMA_URL}/api/generate"

    log(
        "Sending to Ollama "
        f"model={OLLAMA_MODEL}, chars={len(trimmed_text)}, "
        f"num_ctx={OLLAMA_NUM_CTX}, timeout={OLLAMA_READ_TIMEOUT_SECONDS}s"
    )

    response = requests.post(
        url,
        json=payload,
        timeout=(OLLAMA_CONNECT_TIMEOUT_SECONDS, OLLAMA_READ_TIMEOUT_SECONDS),
    )

    if response.status_code >= 400:
        raise RuntimeError(
            f"Ollama API failed: {response.status_code} {response.text}"
        )

    data = response.json()

    if "response" in data:
        return clean_summary_text(str(data["response"]).strip())

    return clean_summary_text(json.dumps(data, ensure_ascii=False, indent=2))


# ============================================================
# GOTENBERG FUNCTIONS
# ============================================================

def check_gotenberg_available() -> None:
    url = f"{GOTENBERG_URL}/health"

    try:
        response = requests.get(url, timeout=20)
    except Exception as e:
        raise RuntimeError(
            f"Could not connect to Gotenberg at {GOTENBERG_URL}. Error: {e}"
        )

    if response.status_code >= 400:
        raise RuntimeError(
            f"Gotenberg health check failed: {response.status_code} {response.text[:500]}"
        )

    log(f"Gotenberg reachable: {GOTENBERG_URL}")


def convert_to_pdf_with_gotenberg(input_path: Path) -> Path:
    if not input_path.exists():
        raise FileNotFoundError(f"Attachment not found: {input_path}")

    original_hash_short = file_sha256(input_path)[:12]
    clean_stem = safe_filename(input_path.stem[:80])
    output_pdf = CONVERTED_PDF_DIR / f"{clean_stem}_{original_hash_short}.pdf"

    if output_pdf.exists() and output_pdf.stat().st_size > 0:
        log(f"Using existing converted PDF: {output_pdf}")
        return output_pdf

    url = f"{GOTENBERG_URL}/forms/libreoffice/convert"

    log(f"Converting with Gotenberg: {input_path.name} -> {output_pdf.name}")

    with open(input_path, "rb") as f:
        files = {
            "files": (input_path.name, f, "application/octet-stream")
        }

        response = requests.post(
            url,
            files=files,
            timeout=GOTENBERG_TIMEOUT_SECONDS,
        )

    if response.status_code != 200:
        raise RuntimeError(
            "Gotenberg conversion failed.\n"
            f"URL: {url}\n"
            f"HTTP: {response.status_code}\n"
            f"Response: {response.text[:1000]}"
        )

    content = response.content or b""

    if not content.startswith(b"%PDF"):
        raise RuntimeError(
            "Gotenberg returned data, but it does not look like a PDF. "
            f"First bytes: {content[:50]!r}"
        )

    output_pdf.write_bytes(content)

    if output_pdf.stat().st_size == 0:
        raise RuntimeError(f"Converted PDF is empty: {output_pdf}")

    log(f"Converted PDF saved: {output_pdf} ({output_pdf.stat().st_size} bytes)")
    return output_pdf


# ============================================================
# IMAP FUNCTIONS
# ============================================================

def connect_imap() -> imaplib.IMAP4_SSL:
    if not EMAIL_USER:
        raise RuntimeError("EMAIL_USER is empty.")

    if not EMAIL_PASSWORD:
        raise RuntimeError(
            "EMAIL_PASSWORD is empty. Export Gmail App Password before running."
        )

    if IMAP_HOST == "imap.gmail.com" and " " in EMAIL_PASSWORD:
        raise RuntimeError(
            "Gmail App Password contains spaces. Export it without spaces."
        )

    log(f"Connecting to IMAP {IMAP_HOST}:{IMAP_PORT}")
    log(f"Email user: {EMAIL_USER}")
    log(f"Password length: {len(EMAIL_PASSWORD)}")

    mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)

    try:
        mail.login(EMAIL_USER, EMAIL_PASSWORD)
    except imaplib.IMAP4.error as e:
        raise RuntimeError(
            "Gmail IMAP login failed.\n"
            "Possible reasons:\n"
            "1. Wrong Gmail App Password\n"
            "2. Gmail IMAP is disabled\n"
            "3. 2-Step Verification is not enabled\n"
            "4. App Password was copied incorrectly\n"
            "5. App Password was created for another Google account\n\n"
            f"Original error: {e}"
        )

    log("IMAP login successful")
    return mail


def search_emails(mail: imaplib.IMAP4_SSL) -> List[bytes]:
    status, _ = mail.select(MAIL_FOLDER)

    if status != "OK":
        raise RuntimeError(f"Could not select folder: {MAIL_FOLDER}")

    search_query = "UNSEEN" if ONLY_UNREAD else "ALL"

    status, data = mail.search(None, search_query)

    if status != "OK":
        raise RuntimeError("Email search failed")

    email_ids = data[0].split()
    email_ids = list(reversed(email_ids))
    limited_ids = email_ids[:MAX_EMAILS_TO_CHECK]

    log(f"Total matched emails: {len(email_ids)}")
    log(f"Checking latest emails: {len(limited_ids)}")

    return limited_ids


def fetch_message(
    mail: imaplib.IMAP4_SSL,
    email_id: bytes
) -> Optional[email.message.Message]:
    status, data = mail.fetch(email_id, "(RFC822)")

    if status != "OK":
        log(f"Could not fetch email id: {email_id}")
        return None

    if not data or not data[0]:
        return None

    raw_email = data[0][1]
    msg = email.message_from_bytes(raw_email)
    return msg


def message_matches_filters(msg: email.message.Message) -> bool:
    subject = decode_mime_words(msg.get("Subject", "")).lower()
    sender = decode_mime_words(msg.get("From", "")).lower()

    if SUBJECT_KEYWORD and SUBJECT_KEYWORD not in subject:
        return False

    if SENDER_FILTER and SENDER_FILTER not in sender:
        return False

    return True


def mark_email_seen(mail: imaplib.IMAP4_SSL, email_id: bytes) -> None:
    try:
        mail.store(email_id, "+FLAGS", "\\Seen")
    except Exception as e:
        log(f"Could not mark email as read: {e}")


# ============================================================
# ATTACHMENT FUNCTIONS
# ============================================================

def save_attachments_from_message(
    msg: email.message.Message,
    email_id_text: str,
    subject: str
) -> List[Path]:
    saved_files: List[Path] = []

    for part in msg.walk():
        if part.is_multipart():
            continue

        filename = part.get_filename()
        content_disposition = part.get_content_disposition()

        # Some clients mark real attachments as "inline" but still include a filename.
        if not filename:
            continue

        if content_disposition not in ("attachment", "inline", None):
            continue

        filename = decode_mime_words(filename)
        ext = Path(filename).suffix.lower()

        if not should_attempt_attachment(filename):
            continue

        if ATTACHMENT_NAME_KEYWORD and ATTACHMENT_NAME_KEYWORD not in filename.lower():
            log(f"Skipping attachment keyword not matched: {filename}")
            continue

        payload = part.get_payload(decode=True)

        if not payload:
            log(f"Empty attachment skipped: {filename}")
            continue

        timestamp = now_file_string()
        clean_subject = safe_filename(subject[:60])
        clean_filename = safe_filename(filename)

        output_filename = (
            f"{timestamp}_email_{email_id_text}_{clean_subject}_{clean_filename}"
        )

        output_path = DOWNLOAD_DIR / output_filename

        with open(output_path, "wb") as f:
            f.write(payload)

        saved_files.append(output_path)
        log(f"Downloaded attachment: {output_path}")

    return saved_files


# ============================================================
# TEXT EXTRACTION FUNCTIONS
# ============================================================

def extract_text_from_pdf(file_path: Path) -> str:
    reader = PdfReader(str(file_path))
    pages = []

    for i, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        if text.strip():
            pages.append(f"\n--- Page {i} ---\n{text.strip()}")

    return "\n\n".join(pages)


def extract_text_from_txt(file_path: Path) -> str:
    for enc in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return file_path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue

    return file_path.read_text(encoding="latin-1", errors="ignore")


def prepare_file_for_text_extraction(original_file_path: Path) -> Tuple[Path, str, Optional[Path]]:
    """
    Returns:
        extraction_file_path, conversion_method, converted_pdf_path

    Flow:
    - .pdf          -> extract PDF directly
    - .txt/.csv...  -> extract text directly
    - office files  -> convert to PDF with Gotenberg, then extract PDF text
    - unknown files -> optionally try Gotenberg, unless blocked
    """
    ext = original_file_path.suffix.lower()

    if ext in PDF_EXTENSIONS:
        return original_file_path, "direct_pdf", None

    if ext in DIRECT_TEXT_EXTENSIONS:
        return original_file_path, "direct_text", None

    if ext in GOTENBERG_OFFICE_EXTENSIONS:
        converted_pdf = convert_to_pdf_with_gotenberg(original_file_path)
        return converted_pdf, "gotenberg_to_pdf", converted_pdf

    if PROCESS_UNKNOWN_ATTACHMENTS_WITH_GOTENBERG and ext not in BLOCKED_EXTENSIONS:
        converted_pdf = convert_to_pdf_with_gotenberg(original_file_path)
        return converted_pdf, "gotenberg_unknown_to_pdf", converted_pdf

    raise ValueError(f"Unsupported file type: {ext}")


def extract_text(file_path: Path) -> str:
    ext = file_path.suffix.lower()

    if ext == ".pdf":
        return extract_text_from_pdf(file_path)

    if ext in DIRECT_TEXT_EXTENSIONS:
        return extract_text_from_txt(file_path)

    raise ValueError(f"Unsupported extraction file type: {ext}")


# ============================================================
# SUMMARY SAVE FUNCTIONS
# ============================================================

def save_summary_files(
    summary: str,
    original_file_path: Path,
    extraction_file_path: Path,
    email_subject: str,
    email_sender: str,
    attachment_hash: str,
    email_id_text: str,
    conversion_method: str,
    converted_pdf_path: Optional[Path],
) -> Tuple[Path, Path, Dict[str, Any]]:
    timestamp = now_file_string()
    processed_time = now_string()
    clean_name = safe_filename(original_file_path.stem[:80])

    txt_summary_path = SUMMARY_DIR / f"{timestamp}_{clean_name}_summary.txt"
    json_summary_path = JSON_SUMMARY_DIR / f"{timestamp}_{clean_name}_summary.json"

    converted_pdf_file = str(converted_pdf_path) if converted_pdf_path else ""
    converted_pdf_file_name = converted_pdf_path.name if converted_pdf_path else ""

    record = {
        "processed_time": processed_time,
        "email_id": email_id_text,
        "email_subject": email_subject,
        "email_sender": email_sender,

        # Original attachment
        "attachment_file": str(original_file_path),
        "attachment_file_name": original_file_path.name,
        "attachment_sha256": attachment_hash,

        # File actually used for text extraction
        "extraction_file": str(extraction_file_path),
        "extraction_file_name": extraction_file_path.name,
        "conversion_method": conversion_method,
        "gotenberg_url": GOTENBERG_URL,
        "converted_pdf_file": converted_pdf_file,
        "converted_pdf_file_name": converted_pdf_file_name,

        "summary_file": str(txt_summary_path),
        "summary_json_file": str(json_summary_path),
        "ollama_url": OLLAMA_URL,
        "ollama_model": OLLAMA_MODEL,
        "summary": summary,
        "summary_markdown": "",
        "summary_html": "",
    }

    record["summary_markdown"] = summary_to_markdown_card(record)
    record["summary_html"] = summary_to_html_card(record)

    txt_content = f"""Processed Time: {processed_time}
Email Subject: {email_subject}
Email Sender: {email_sender}
Original Attachment File: {original_file_path.name}
Extraction File: {extraction_file_path.name}
Conversion Method: {conversion_method}
Gotenberg URL: {GOTENBERG_URL}
Converted PDF: {converted_pdf_file_name or "N/A"}
Attachment SHA256: {attachment_hash}
Ollama URL: {OLLAMA_URL}
Ollama Model: {OLLAMA_MODEL}

==================== CHARITY / AID VERIFICATION SUMMARY ====================

{summary}
"""

    txt_summary_path.write_text(txt_content, encoding="utf-8")
    write_json_file(json_summary_path, record)

    index = load_summary_index()

    # avoid duplicate hash in index
    index = [
        item for item in index
        if item.get("attachment_sha256") != attachment_hash
    ]
    index.insert(0, record)
    save_summary_index(index)

    return txt_summary_path, json_summary_path, record


# ============================================================
# PROCESSOR
# ============================================================

def process_emails() -> Dict[str, Any]:
    check_ollama_available()

    if CHECK_GOTENBERG_ON_START:
        check_gotenberg_available()

    processed_db = load_processed_db()
    processed_count = 0
    skipped_duplicate_count = 0
    failed_count = 0

    processed_records: List[Dict[str, Any]] = []

    mail = connect_imap()

    try:
        email_ids = search_emails(mail)

        for email_id in email_ids:
            if processed_count >= MAX_ATTACHMENTS_PER_RUN:
                log(f"Reached MAX_ATTACHMENTS_PER_RUN={MAX_ATTACHMENTS_PER_RUN}")
                break

            msg = fetch_message(mail, email_id)

            if not msg:
                continue

            subject = decode_mime_words(msg.get("Subject", ""))
            sender = decode_mime_words(msg.get("From", ""))

            if not message_matches_filters(msg):
                continue

            email_id_text = email_id.decode("utf-8", errors="ignore")

            print("\n" + "=" * 90)
            print(f"Matched Email ID: {email_id_text}")
            print(f"Subject: {subject}")
            print(f"From: {sender}")
            print("=" * 90, flush=True)

            attachments = save_attachments_from_message(
                msg=msg,
                email_id_text=email_id_text,
                subject=subject
            )

            if not attachments:
                log("No processable attachment found in this email.")
                continue

            email_had_processed_attachment = False

            for original_file_path in attachments:
                if processed_count >= MAX_ATTACHMENTS_PER_RUN:
                    break

                try:
                    attachment_hash = file_sha256(original_file_path)

                    if attachment_hash in processed_db:
                        log(f"Duplicate already processed, skipping: {original_file_path.name}")
                        skipped_duplicate_count += 1
                        continue

                    extraction_file_path, conversion_method, converted_pdf_path = (
                        prepare_file_for_text_extraction(original_file_path)
                    )

                    log(
                        f"Extracting text from: {extraction_file_path.name} "
                        f"(method={conversion_method})"
                    )
                    extracted_text = extract_text(extraction_file_path)

                    log(f"Extracted characters: {len(extracted_text)}")

                    if not extracted_text.strip():
                        log("No readable text found, skipping LLM summary.")
                        failed_count += 1
                        continue

                    summary = summarize_with_ollama(
                        text=extracted_text,
                        filename=original_file_path.name,
                        email_subject=subject,
                        email_sender=sender,
                    )

                    txt_summary_path, json_summary_path, record = save_summary_files(
                        summary=summary,
                        original_file_path=original_file_path,
                        extraction_file_path=extraction_file_path,
                        email_subject=subject,
                        email_sender=sender,
                        attachment_hash=attachment_hash,
                        email_id_text=email_id_text,
                        conversion_method=conversion_method,
                        converted_pdf_path=converted_pdf_path,
                    )

                    processed_db[attachment_hash] = {
                        "processed_time": record["processed_time"],
                        "email_id": email_id_text,
                        "email_subject": subject,
                        "email_sender": sender,
                        "attachment_file": str(original_file_path),
                        "extraction_file": str(extraction_file_path),
                        "conversion_method": conversion_method,
                        "gotenberg_url": GOTENBERG_URL,
                        "summary_file": str(txt_summary_path),
                        "summary_json_file": str(json_summary_path),
                        "ollama_url": OLLAMA_URL,
                        "ollama_model": OLLAMA_MODEL,
                    }

                    save_processed_db(processed_db)

                    processed_count += 1
                    email_had_processed_attachment = True
                    processed_records.append(record)

                    print("\nSUMMARY")
                    print("-" * 90)
                    print(summary)
                    print("-" * 90)
                    log(f"Summary saved: {txt_summary_path}")
                    log(f"JSON saved: {json_summary_path}")

                except Exception as e:
                    failed_count += 1
                    log(f"Failed to process attachment {original_file_path.name}: {e}")

            if MARK_EMAIL_AS_READ_AFTER_PROCESS and email_had_processed_attachment:
                mark_email_seen(mail, email_id)
                log("Email marked as read.")

        return {
            "ok": True,
            "processed_new_attachments": processed_count,
            "skipped_duplicate_attachments": skipped_duplicate_count,
            "failed_attachments": failed_count,
            "downloaded_files_folder": str(DOWNLOAD_DIR),
            "converted_pdf_folder": str(CONVERTED_PDF_DIR),
            "summary_folder": str(SUMMARY_DIR),
            "summary_json_folder": str(JSON_SUMMARY_DIR),
            "processed_db_file": str(PROCESSED_DB_FILE),
            "summary_index_file": str(SUMMARY_INDEX_FILE),
            "records": processed_records,
        }

    finally:
        try:
            mail.logout()
            log("IMAP logout successful")
        except Exception:
            pass


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    print("=" * 90)
    print("Gmail IMAP Attachment Fetch + Gotenberg PDF Convert + Ollama Summary")
    print("=" * 90)
    print(f"Email user: {EMAIL_USER}")
    print(f"IMAP host: {IMAP_HOST}:{IMAP_PORT}")
    print(f"Mail folder: {MAIL_FOLDER}")
    print(f"Ollama URL: {OLLAMA_URL}")
    print(f"Ollama model: {OLLAMA_MODEL}")
    print(f"Gotenberg URL: {GOTENBERG_URL}")
    print(f"Only unread: {ONLY_UNREAD}")
    print(f"Max emails to check: {MAX_EMAILS_TO_CHECK}")
    print(f"Max attachments per run: {MAX_ATTACHMENTS_PER_RUN}")
    print(f"Max text chars for LLM: {MAX_TEXT_CHARS_FOR_LLM}")
    print(f"Ollama num_ctx: {OLLAMA_NUM_CTX}")
    print(f"Ollama read timeout: {OLLAMA_READ_TIMEOUT_SECONDS}")
    print(f"Gotenberg timeout: {GOTENBERG_TIMEOUT_SECONDS}")
    print(f"Process unknown attachments with Gotenberg: {PROCESS_UNKNOWN_ATTACHMENTS_WITH_GOTENBERG}")
    print("=" * 90, flush=True)

    result = process_emails()

    print("\n" + "=" * 90)
    print("RUN COMPLETED")
    print("=" * 90)
    print(f"Processed new attachments: {result['processed_new_attachments']}")
    print(f"Skipped duplicate attachments: {result['skipped_duplicate_attachments']}")
    print(f"Failed attachments: {result['failed_attachments']}")
    print(f"Downloaded files folder: {result['downloaded_files_folder']}")
    print(f"Converted PDF folder: {result['converted_pdf_folder']}")
    print(f"Summary folder: {result['summary_folder']}")
    print(f"Summary JSON folder: {result['summary_json_folder']}")
    print(f"Processed DB file: {result['processed_db_file']}")
    print(f"Summary index file: {result['summary_index_file']}", flush=True)


if __name__ == "__main__":
    main()
