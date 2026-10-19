"""Policy pages render outside admin without admin dashboard context."""
from pathlib import Path
from types import SimpleNamespace
from django.http import HttpResponse
from django.template import Context, Engine
from django.test import SimpleTestCase, override_settings
from django.urls import include, path

urlpatterns = [path("policy-ai/", include(([
    path("", lambda request: HttpResponse(), name="chat"),
    path("studio/", lambda request: HttpResponse(), name="studio"),
], "openai_policy"), namespace="openai_policy"))]


@override_settings(ROOT_URLCONF=__name__)
class PolicyTemplateTests(SimpleTestCase):
    def test_policy_pages_render_without_admin_log_entries(self):
        root = Path(__file__).resolve().parents[2]
        engine = Engine(dirs=[str(root / "openai_policy/templates")])
        for page, title in (("chat", "Policy Assistant | BURO Insight AI"), ("studio", "Policy Studio | BURO Insight AI")):
            for admin in (False, True):
                with self.subTest(page=page, admin=admin):
                    context = Context({"request": SimpleNamespace(user=SimpleNamespace(is_superuser=admin)),
                                       "perms": {}, "csrf_token": "test-token"})
                    html = engine.get_template("openai_policy/" + page + ".html").render(context)
                    self.assertIn("<title>" + title + "</title>", html)
                    self.assertIn("<!doctype html>", html)
                    if page == "chat" and admin:
                        self.assertIn("/policy-ai/studio/", html)
