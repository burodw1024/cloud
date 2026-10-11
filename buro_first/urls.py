# project/urls.py
from django.contrib import admin
from django.urls import path, include
#from chat.views import upload_jsonl
from django.contrib.auth import views as auth_views
from django.conf import settings
from django.conf.urls.static import static


urlpatterns = [
    path('agentic/', include('agentic_ai.urls')),
    path("admin/", admin.site.urls),

    # User-friendly auth
    path("login/",  auth_views.LoginView.as_view(template_name="chat/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(next_page="/"), name="logout"),



    # All chat / api / admin-tools routing
    path("", include("chat.urls")),

    # JSONL upload (staff-only, API-style)
    path("api/files/upload", include("chat.urls")),

   #added new app for conversational chatting
   path("workflow/", include("workflows.urls")),
   path("", include("django_prometheus.urls")),

   path(
    "policy-ai/",
    include("openai_policy.urls")
),



    path('hr-notices/', include('hr_notice_rag.urls')),] 
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

