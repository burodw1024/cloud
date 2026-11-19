from django.apps import AppConfig


class AgenticAiConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "agentic_ai"
    verbose_name = "BURO Insight AI"

    def ready(self):
        from . import checks  # noqa: F401
        from django.apps import apps
        if apps.is_installed('django_prometheus'):
            from .scheduler_metrics import register
            register()
