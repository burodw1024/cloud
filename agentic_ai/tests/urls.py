from django.contrib.auth.views import LogoutView
from django.urls import include, path

urlpatterns = [
    path("logout/", LogoutView.as_view(next_page="/"), name="logout"),
    path("agentic/", include("agentic_ai.urls")),
]
