from django.urls import path
from . import views

app_name = "hr_notice_rag"

urlpatterns = [
    path("", views.notice_list, name="list"),
    path("upload/", views.upload_notice, name="upload"),
    path("<int:pk>/", views.notice_detail, name="detail"),
    path("<int:pk>/reindex/", views.reindex_notice, name="reindex"),
    path("search/", views.search_notice_context, name="search"),
]
