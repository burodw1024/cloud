SECRET_KEY = "isolated-mcp-tests"
INSTALLED_APPS = ["django.contrib.auth", "django.contrib.contenttypes", "agentic_ai"]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
INSTALLED_APPS += ["django.contrib.sessions"]
MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
]
ROOT_URLCONF = "agentic_ai.tests.urls"
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]

TEMPLATES = [{"BACKEND": "django.template.backends.django.DjangoTemplates", "APP_DIRS": True,
              "OPTIONS": {"context_processors": ["django.contrib.auth.context_processors.auth", "django.template.context_processors.request"]}}]
