from django.apps import AppConfig


class AgenticAiConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "agentic_ai"
    verbose_name = "Enterprise Agentic AI"

    def ready(self):
        from . import checks  # noqa: F401
