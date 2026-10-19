from django.urls import path
from . import views
from .setup_views import query_page, setup_page

app_name = "agentic_ai"

urlpatterns = [
    path('api/website-query/', views.public_website_query_api, name='public_website_query_api'),
    path("", query_page, name="query_page"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("setup/", setup_page, name="setup"),
    path("api/query/", views.agentic_query_api, name="agentic_query_api"),
    path("api/logs/", views.logs_api, name="logs_api"),
]
