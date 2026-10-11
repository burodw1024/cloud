from django.urls import path
from . import views

app_name = "agentic_ai"

urlpatterns = [
    path('api/website-query/', views.public_website_query_api, name='public_website_query_api'),
    path("", views.dashboard, name="dashboard"),
    path("api/query/", views.agentic_query_api, name="agentic_query_api"),
    path("api/logs/", views.logs_api, name="logs_api"),
]
