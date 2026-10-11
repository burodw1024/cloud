# chat/email_summary_agent.py
from __future__ import annotations

import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any, Dict


APP_DIR = Path(__file__).resolve().parent

ENV_FILE = APP_DIR / "gmail_ollama_env.sh"
RUN_SCRIPT = APP_DIR / "run_email_summarizer.sh"
SUMMARIZER_SCRIPT = APP_DIR / "email_attachment_llm_summarizer.py"

LOG_DIR = Path(os.getenv("EMAIL_SUMMARY_LOG_DIR", str(APP_DIR / "logs")))
LOG_FILE = LOG_DIR / "email_summary_agent.log"
LOCK_FILE = LOG_DIR / "email_summary_agent.lock"

LOCK_MAX_AGE_SECONDS = int(os.getenv("EMAIL_SUMMARY_LOCK_MAX_AGE_SECONDS", "21600"))


def _remove_stale_lock() -> None:
    if not LOCK_FILE.exists():
        return

    try:
        age_seconds = time.time() - LOCK_FILE.stat().st_mtime
        if age_seconds > LOCK_MAX_AGE_SECONDS:
            LOCK_FILE.unlink()
    except Exception:
        pass


def is_email_summary_agent_running() -> bool:
    _remove_stale_lock()
    return LOCK_FILE.exists()


def start_email_summary_agent() -> Dict[str, Any]:
    """
    Start Gmail email attachment summarizer as a backend agent.

    Important:
    - This module does not check user permissions.
    - views.py must check RBAC/role before calling this function.
    - The process is started in background so the UI/chat request does not wait
      for Gmail download, file extraction, and Ollama summary generation.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    _remove_stale_lock()

    if LOCK_FILE.exists():
        return {
            "ok": False,
            "status": "running",
            "message": "Email summary agent is already running.",
            "log_file": str(LOG_FILE),
            "lock_file": str(LOCK_FILE),
        }

    if not RUN_SCRIPT.exists() and not SUMMARIZER_SCRIPT.exists():
        return {
            "ok": False,
            "status": "missing_script",
            "message": f"Missing summarizer script: {SUMMARIZER_SCRIPT}",
            "log_file": str(LOG_FILE),
            "lock_file": str(LOCK_FILE),
        }

    app_dir_q = shlex.quote(str(APP_DIR))
    env_file_q = shlex.quote(str(ENV_FILE))
    run_script_q = shlex.quote(str(RUN_SCRIPT))
    summarizer_script_q = shlex.quote(str(SUMMARIZER_SCRIPT))
    lock_file_q = shlex.quote(str(LOCK_FILE))
    python_bin_q = shlex.quote(os.getenv("PYTHON_BIN", "python"))

    command = f"""
    set -e
    cd {app_dir_q}

    trap 'rm -f {lock_file_q}' EXIT

    if [ -f {env_file_q} ]; then
        source {env_file_q}
    fi

    if [ -f {run_script_q} ]; then
        bash {run_script_q}
    else
        {python_bin_q} {summarizer_script_q}
    fi
    """

    try:
        LOCK_FILE.write_text(
            f"running since {time.strftime('%Y-%m-%d %H:%M:%S')}",
            encoding="utf-8",
        )

        with open(LOG_FILE, "a", encoding="utf-8") as log:
            log.write("\n==============================\n")
            log.write(time.strftime("[%Y-%m-%d %H:%M:%S] "))
            log.write("Email summary agent started\n")
            log.write(f"APP_DIR={APP_DIR}\n")
            log.write(f"ENV_FILE={ENV_FILE}\n")
            log.write(f"RUN_SCRIPT={RUN_SCRIPT}\n")
            log.write(f"SUMMARIZER_SCRIPT={SUMMARIZER_SCRIPT}\n")

            subprocess.Popen(
                ["bash", "-lc", command],
                stdout=log,
                stderr=log,
                cwd=str(APP_DIR),
                env=os.environ.copy(),
                start_new_session=True,
            )

        return {
            "ok": True,
            "status": "started",
            "message": "Email summary agent started successfully.",
            "log_file": str(LOG_FILE),
            "lock_file": str(LOCK_FILE),
        }

    except Exception as e:
        try:
            if LOCK_FILE.exists():
                LOCK_FILE.unlink()
        except Exception:
            pass

        return {
            "ok": False,
            "status": "error",
            "message": str(e),
            "log_file": str(LOG_FILE),
            "lock_file": str(LOCK_FILE),
        }


def get_email_summary_agent_status() -> Dict[str, Any]:
    running = is_email_summary_agent_running()

    return {
        "ok": True,
        "running": running,
        "message": "running" if running else "not running",
        "log_file": str(LOG_FILE),
        "lock_file": str(LOCK_FILE),
    }
