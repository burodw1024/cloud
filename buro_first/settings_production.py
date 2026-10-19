"""Opt-in production settings; never use the development profile for deployment."""
import os
from pathlib import Path
from django.core.exceptions import ImproperlyConfigured
from .settings import *  # noqa: F403,F401


def required(name):
    value = os.environ.get(name, "").strip()
    if not value or value.startswith("REPLACE_"):
        raise ImproperlyConfigured(f"Production requires {name}.")
    return value


DEBUG = False
SECRET_KEY = required("DJANGO_SECRET_KEY")
if len(SECRET_KEY) < 50 or SECRET_KEY.startswith("django-insecure-"):
    raise ImproperlyConfigured("Use a strong unique DJANGO_SECRET_KEY of at least 50 characters.")
ALLOWED_HOSTS = [host.strip() for host in required("DJANGO_ALLOWED_HOSTS").split(",") if host.strip()]
if not ALLOWED_HOSTS or "*" in ALLOWED_HOSTS:
    raise ImproperlyConfigured("Production requires explicit DJANGO_ALLOWED_HOSTS.")
CSRF_TRUSTED_ORIGINS = [origin.strip() for origin in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if origin.strip()]
if any(not origin.startswith("https://") for origin in CSRF_TRUSTED_ORIGINS):
    raise ImproperlyConfigured("Production CSRF origins must use HTTPS.")

# One replica on a persistent local/block filesystem; no external DB/cache service.
DATA_DIR = Path(os.environ.get("DJANGO_DATA_DIR", "/data"))
DATABASES = {"default": {
    "ENGINE": "django.db.backends.sqlite3",
    "NAME": DATA_DIR / "db.sqlite3",
    "OPTIONS": {"timeout": 20, "transaction_mode": "IMMEDIATE"},
}}
CACHES = {"default": {
    "BACKEND": "agentic_ai.sqlite_cache.SQLiteCache",
    "LOCATION": str(DATA_DIR / "cache.sqlite3"), "KEY_PREFIX": "buro:" + os.environ.get("AGENT_RELEASE_ID", "legacy"),
}}
MEDIA_ROOT = DATA_DIR / "uploads"
# Base settings calculate this before MEDIA_ROOT is overridden.
POLICY_WORK_DIR = str(DATA_DIR / "policy_rag")
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = True
SECURE_SSL_REDIRECT = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = os.environ.get("DJANGO_HSTS_INCLUDE_SUBDOMAINS", "false").lower() == "true"
SECURE_HSTS_PRELOAD = os.environ.get("DJANGO_HSTS_PRELOAD", "false").lower() == "true"
X_FRAME_OPTIONS = "DENY"
# The ingress must strip/replace any client-supplied forwarding headers.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
LOGGING = {
    "version": 1, "disable_existing_loggers": False,
    "formatters": {"standard": {"format": "{asctime} {levelname} {name} {message}", "style": "{"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "standard"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {"httpx": {"level": "WARNING"}, "httpcore": {"level": "WARNING"}},
}

# Collected by the Kubernetes init container and served by the TLS proxy.
STATIC_URL = "/static/"
STATIC_ROOT = Path(BASE_DIR) / "staticfiles"
