from django.urls import path
from .views import workflow_message_api, workflow_upload_api

urlpatterns = [
    path("message/", workflow_message_api, name="workflow_message_api"),
    path("upload/", workflow_upload_api, name="workflow_upload_api"),
]
