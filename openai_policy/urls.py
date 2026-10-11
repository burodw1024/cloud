from django.urls import path
from . import views
app_name = "openai_policy"
urlpatterns = [
    path("", views.chat_page, name="chat"),
    path("studio/", views.studio_page, name="studio"),
    path("api/chat", views.chat_api, name="chat_api"),
    path("api/tts", views.tts_api, name="tts_api"),
    path("api/admin/config", views.admin_config, name="admin_config"),
    path("api/admin/files", views.admin_files, name="admin_files"),
    path("api/admin/upsert", views.admin_upsert, name="admin_upsert"),
    path("api/admin/files/<str:file_id>", views.admin_file_item, name="admin_file_item"),
    path("api/admin/search-test", views.search_test, name="search_test"),
    path("api/admin/preprocess/config", views.preprocess_config, name="preprocess_config"),
    path("api/admin/preprocess", views.preprocess_create, name="preprocess_create"),
    path("api/admin/preprocess/jobs", views.preprocess_jobs, name="preprocess_jobs"),
    path("api/admin/preprocess/<str:job_id>", views.preprocess_item, name="preprocess_item"),
    path("api/admin/preprocess/<str:job_id>/approve", views.preprocess_approve, name="preprocess_approve"),
    path("api/admin/preprocess/<str:job_id>/artifact/<str:artifact>", views.preprocess_artifact, name="preprocess_artifact"),
    path("api/admin/effective/build", views.effective_build, name="effective_build"),
    path("api/admin/effective/builds", views.effective_builds, name="effective_builds"),
    path("api/admin/effective/<str:build_id>", views.effective_item, name="effective_item"),
    path("api/admin/effective/<str:build_id>/artifact/<str:artifact>", views.effective_artifact, name="effective_artifact"),
    path("api/admin/effective/<str:build_id>/upsert", views.effective_publish, name="effective_publish"),
]
