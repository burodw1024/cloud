from agentic_ai.access_readiness import access_readiness
from agentic_ai.user_access import user_access
from django.urls import include, path
from django.contrib.auth.views import LogoutView
from agentic_ai.api import agentic_query_api
from agentic_ai.bangla_summary import summary_api
from agentic_ai.whatsapp import whatsapp_api
from agentic_ai.setup_views import query_page, setup_page
from agentic_ai.review import dashboard, logs_api
from agentic_ai.module_admin import module_manager
from agentic_ai.access_admin import access_manager, directory_manager
patterns = [
    path("api/summary/", summary_api, name="summary_api"),
    path("api/whatsapp/", whatsapp_api, name="whatsapp_api"),
    path("api/query/", agentic_query_api, name="agentic_query_api"),
    path("", query_page, name="query_page"),
    path("setup/", setup_page, name="setup"),
    path("api/access-readiness/", access_readiness, name="access_readiness"),
    path("user-access/", user_access, name="user_access"),
    path("modules/", module_manager, name="modules"),
    path('data-access/', access_manager, name='data_access'),
    path('data-access/directory/', directory_manager, name='access_directory'),
    path("dashboard/", dashboard, name="dashboard"),
    path("api/logs/", logs_api, name="logs_api"),
]
urlpatterns = [path("logout/", LogoutView.as_view(next_page="/"), name="logout"),
               path("agentic/", include((patterns, "agentic_ai"), namespace="agentic_ai"))]
