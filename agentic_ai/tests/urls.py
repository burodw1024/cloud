from django.urls import include, path
from django.contrib.auth.views import LogoutView
from agentic_ai.api import agentic_query_api
from agentic_ai.bangla_summary import summary_api
from agentic_ai.whatsapp import whatsapp_api
from agentic_ai.setup_views import query_page, setup_page
patterns = [
    path("api/summary/", summary_api, name="summary_api"),
    path("api/whatsapp/", whatsapp_api, name="whatsapp_api"),
    path("api/query/", agentic_query_api, name="agentic_query_api"),
    path("", query_page, name="query_page"),
    path("setup/", setup_page, name="setup"),
    path("dashboard/", query_page, name="dashboard"),
]
urlpatterns = [path("logout/", LogoutView.as_view(next_page="/"), name="logout"),
               path("agentic/", include((patterns, "agentic_ai"), namespace="agentic_ai"))]
