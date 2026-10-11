from . import zypl_cashflow_logs_views
from . import zypl_cashflow_scoring_views

from .zypl_cashflow_batch_views import (
    zypl_cashflow_score_file_from_uploaded_file_api,
)


# chat/urls.py
from django.urls import path
from django.views.decorators.csrf import csrf_exempt

from . import views

from .file_upload import upload_file

from .views_hr_router import hr_ask
from .views_policy import policy_ask, policy_ask_stream
from .views_core import core_ask
from .views_cashflow_router import cashflow_query
from .views_public import public_notice_search, public_notice_get
from .views_upload_doc import upload_doc_ask

from .qdrant_admin_views import qdrant_collections_admin
from .views_files import upload_jsonl

from .views_policy_admin import (
    policy_topics_list,
    policy_topic_new,
    policy_topic_edit,
    policy_topic_delete,
    policy_topic_clone,
    policy_topics_bulk_import,
)

from .views_notice import (
    notice_chat_page,
    notice_ask,
    notice_ask_stream,
)

from .customer_support_api import (
    customer_support_ask,
    customer_support_create_ticket,
    customer_support_ticket_status,
    customer_support_verify,
    customer_support_installment_status,
    customer_support_loan_summary,
    customer_support_savings_summary,
    customer_support_branch_info,
    customer_support_escalate_ticket,
)

from .views_settings import settings_dashboard
#from .views_voice import voice_to_text
from . import views_voice
# Existing admin tools
from . import views_website_sync_admin
from . import views_qdrant_copy_restore


urlpatterns = [
    path("api/zypl/cashflow/scoring-logs/", zypl_cashflow_logs_views.zypl_cashflow_scoring_logs_api, name="zypl_cashflow_scoring_logs_api"),
    path("api/zypl/cashflow/scoring-logs/reset/", zypl_cashflow_logs_views.zypl_cashflow_scoring_logs_reset_api, name="zypl_cashflow_scoring_logs_reset_api"),

    path("api/zypl/cashflow/score-agent/", zypl_cashflow_scoring_views.zypl_cashflow_score_agent_api, name="zypl_cashflow_score_agent_api"),
    path("api/zypl/cashflow/reset/", zypl_cashflow_scoring_views.zypl_cashflow_score_reset_api, name="zypl_cashflow_score_reset_api"),


   path(
    "api/zypl/cashflow/score-file/from-uploaded-file",
    zypl_cashflow_score_file_from_uploaded_file_api,
    name="zypl_cashflow_score_file_from_uploaded_file_api",
       ),


    # Main chat page
    path("", views.index, name="index"),

    # Chat APIs
    path("api/chat", views.api_chat, name="api_chat"),
    path("api/auto/ask", views.auto_ask, name="auto_ask"),

    # Direct module endpoints
    path("api/hr/ask", hr_ask, name="hr_ask"),
    path("api/core/ask", core_ask, name="core_ask"),
    path("api/policy/ask", policy_ask, name="policy_ask"),
    path("api/policy/ask-stream", policy_ask_stream, name="policy_ask_stream"),
    path("api/cashflow/query", cashflow_query, name="cashflow_query"),

    # Public notice
    path("public/notice/search", public_notice_search, name="public_notice_search"),
    path("public/notice/get", public_notice_get, name="public_notice_get"),

    # File upload
    path("api/files/upload", upload_file, name="upload_file"),

    # Document mode
    path("api/doc/ask", upload_doc_ask, name="upload_doc_ask"),

    # Admin tools
    path("admin/qdrant/", qdrant_collections_admin, name="qdrant_collections_admin"),
    path("admin/ai-tools/upload-jsonl/", upload_jsonl, name="upload_jsonl"),

    # Admin-tools aliases/menu
    path("admin-tools/qdrant/", qdrant_collections_admin, name="admin_tools_qdrant"),
    path("admin-tools/upload-jsonl/", upload_jsonl, name="admin_tools_upload_jsonl"),
    path("admin-tools/settings/", settings_dashboard, name="admin_tools_settings"),

    # Existing website sync admin page
    path(
        "admin-tools/website-sync/",
        views_website_sync_admin.website_sync_page,
        name="website_sync_page",
    ),

    # Existing Qdrant copy/restore page
    path(
        "admin-tools/qdrant-copy-restore/",
        views_qdrant_copy_restore.qdrant_copy_restore_page,
        name="qdrant_copy_restore_page",
    ),

    # Admin Tools: Policy Topics
    path("admin-tools/policy-topics/", policy_topics_list, name="policy_topics_list"),
    path("admin-tools/policy-topics/new/", policy_topic_new, name="policy_topic_new"),
    path(
        "admin-tools/policy-topics/import/",
        csrf_exempt(policy_topics_bulk_import),
        name="policy_topics_bulk_import",
    ),
    path(
        "admin-tools/policy-topics/<str:topic_id>/delete/",
        policy_topic_delete,
        name="policy_topic_delete",
    ),
    path(
        "admin-tools/policy-topics/<str:topic_id>/clone/",
        policy_topic_clone,
        name="policy_topic_clone",
    ),
    path(
        "admin-tools/policy-topics/<str:topic_id>/",
        policy_topic_edit,
        name="policy_topic_edit",
    ),

    # Notice
    path("notice/", notice_chat_page, name="notice_chat_page"),
    path("api/notice/ask", notice_ask, name="notice_ask"),
    path("api/notice/ask-stream", notice_ask_stream, name="notice_ask_stream"),

    # Customer support
    path("api/customer-support/ask/", customer_support_ask, name="customer_support_ask"),
    path(
        "api/customer-support/create-ticket/",
        customer_support_create_ticket,
        name="customer_support_create_ticket",
    ),
    path(
        "api/customer-support/ticket-status/",
        customer_support_ticket_status,
        name="customer_support_ticket_status",
    ),
    path(
        "api/customer-support/verify/",
        customer_support_verify,
        name="customer_support_verify",
    ),
    path(
        "api/customer-support/installment-status/",
        customer_support_installment_status,
        name="customer_support_installment_status",
    ),
    path(
        "api/customer-support/loan-summary/",
        customer_support_loan_summary,
        name="customer_support_loan_summary",
    ),
    path(
        "api/customer-support/savings-summary/",
        customer_support_savings_summary,
        name="customer_support_savings_summary",
    ),
    path(
        "api/customer-support/branch-info/",
        customer_support_branch_info,
        name="customer_support_branch_info",
    ),
    path(
        "api/customer-support/escalate-ticket/",
        customer_support_escalate_ticket,
        name="customer_support_escalate_ticket",
    ),

    # Voice API
path("api/voice/to-text/", views_voice.voice_to_text, name="voice_to_text"),
path("voice/to-text/", views_voice.voice_to_text, name="voice_to_text_alt"),
path("voice/transcribe/", views_voice.voice_to_text, name="voice_transcribe"),


#Gmail chat  Attahcment
 path(
        "api/email-attachment-summary",
        views.email_attachment_summary_api,
        name="email_attachment_summary_api"
    ),

]

