from django.urls import path
from .views_policy_admin import policy_topics_list, policy_topic_edit

urlpatterns = [
    path("admin/policy-topics/", policy_topics_list, name="policy_topics_list"),
    path("admin/policy-topics/<str:topic_id>/", policy_topic_edit, name="policy_topic_edit"),
]
