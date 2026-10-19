# chat/views_settings.py

from functools import wraps

from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_protect

from chat.dynamic_config import (
    DEFAULT_SETTINGS,
    clear_setting_cache,
    get_available_ollama_models,
    get_setting,
)
from chat.models import SystemSetting


# ---------------------------------------------------------------------
# SECURITY
# ---------------------------------------------------------------------

ROOT_SETTINGS_USERNAME = "root_123"


def root_settings_user_required(view_func):
    """
    Restrict sensitive AI/system settings to the designated root user.

    Protected settings include:
    - Qdrant URLs
    - Ollama URL/models
    - NiFi URLs
    - OpenAI API credentials
    - Gemini API credentials
    - Claude API credentials
    - RAG configuration
    """

    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        user = request.user

        if not user.is_authenticated:
            return redirect_to_login(
                request.get_full_path(),
                login_url=reverse("admin:login"),
            )

        if not (
            user.is_active
            and user.is_staff
            and user.username == ROOT_SETTINGS_USERNAME
        ):
            raise PermissionDenied(
                "You are not allowed to access sensitive system settings."
            )

        return view_func(request, *args, **kwargs)

    return _wrapped_view


# ---------------------------------------------------------------------
# AVAILABLE PROVIDERS
# ---------------------------------------------------------------------

AI_PROVIDER_CHOICES = [
    "ollama",
    "openai",
    "gemini",
    "claude",
]


# ---------------------------------------------------------------------
# MANAGED SETTINGS
# ---------------------------------------------------------------------

MANAGED_SETTINGS = [
    {
        "key": "AGENT_PLANNER_MODEL",
        "label": "Agent Planner Model (Ollama)",
        "description": "Model for structured tool selection. Blank uses the default Ollama model; requires JSON schema output support.",
        "input_type": "text",
        "default": "",
        "group": "Agent Tools",
    },
    {
        "key": "AGENT_PLANNER_TIMEOUT",
        "label": "Agent Planner Timeout (seconds)",
        "description": "Read timeout for tool selection, from 1 to 180 seconds.",
        "input_type": "number",
        "default": "60",
        "group": "Agent Tools",
    },



    # =================================================================
    # GENERAL AI SETTINGS
    # =================================================================

    {
        "key": "AI_DEFAULT_PROVIDER",
        "label": "Default AI Provider",
        "description": (
            "Default provider used for normal AI requests. "
            "Supported: Ollama, OpenAI, Gemini and Claude."
        ),
        "input_type": "select",
        "choices": AI_PROVIDER_CHOICES,
        "default": "ollama",
        "group": "AI Provider",
    },

    {
        "key": "AI_QUERY_REWRITE_PROVIDER",
        "label": "Query Rewrite Provider",
        "description": (
            "Provider used to understand, normalize and rewrite "
            "user questions before RAG retrieval."
        ),
        "input_type": "select",
        "choices": AI_PROVIDER_CHOICES,
        "default": "ollama",
        "group": "AI Provider",
    },

    {
        "key": "AI_QUERY_REWRITE_MODEL",
        "label": "Query Rewrite Model",
        "description": (
            "Model used to rewrite user questions before vector search. "
            "Example: qwen3.5:9b, gemini model, GPT model, etc."
        ),
        "input_type": "text",
        "default": "",
        "group": "AI Provider",
    },

    {
        "key": "AI_ANSWER_PROVIDER",
        "label": "RAG Answer Provider",
        "description": (
            "Provider responsible for generating the final answer "
            "after RAG retrieval."
        ),
        "input_type": "select",
        "choices": AI_PROVIDER_CHOICES,
        "default": "ollama",
        "group": "AI Provider",
    },

    {
        "key": "AI_ANSWER_MODEL",
        "label": "RAG Answer Model",
        "description": (
            "Model used to generate the final answer from retrieved context."
        ),
        "input_type": "text",
        "default": "",
        "group": "AI Provider",
    },


    # =================================================================
    # OLLAMA SETTINGS
    # =================================================================

    {
        "key": "OLLAMA_URL",
        "label": "Ollama URL",
        "description": "Main Ollama API endpoint.",
        "input_type": "text",
        "default": "http://127.0.0.1:11434",
        "group": "Ollama",
    },

    {
        "key": "OLLAMA_MODEL",
        "label": "Default Ollama Model",
        "description": (
            "Default Ollama model used by the whole AI application."
        ),
        "input_type": "ollama_select",
        "default": "",
        "group": "Ollama",
    },

    # -----------------------------------------------------------------
    # Five Local Model Slots
    # -----------------------------------------------------------------

    {
        "key": "OLLAMA_MODEL_QWEN",
        "label": "Qwen Model",
        "description": (
            "Qwen model used by the application. "
            "Example: qwen3.5:9b or qwen3:14b."
        ),
        "input_type": "ollama_select",
        "default": "",
        "group": "Local AI Models",
    },

    {
        "key": "OLLAMA_MODEL_AYA",
        "label": "Aya Model",
        "description": (
            "Aya model used for multilingual/Bangla workloads."
        ),
        "input_type": "ollama_select",
        "default": "",
        "group": "Local AI Models",
    },

    {
        "key": "OLLAMA_MODEL_DEEPSEEK",
        "label": "DeepSeek Model",
        "description": (
            "DeepSeek model used for reasoning workloads."
        ),
        "input_type": "ollama_select",
        "default": "",
        "group": "Local AI Models",
    },

    {
        "key": "OLLAMA_MODEL_GEMMA",
        "label": "Gemma Model",
        "description": (
            "Google Gemma local model."
        ),
        "input_type": "ollama_select",
        "default": "",
        "group": "Local AI Models",
    },

    {
        "key": "OLLAMA_MODEL_FALLBACK",
        "label": "Fallback / Other Local Model",
        "description": (
            "Additional or fallback local Ollama model."
        ),
        "input_type": "ollama_select",
        "default": "",
        "group": "Local AI Models",
    },


    # =================================================================
    # OPENAI
    # =================================================================

    {
        "key": "OPENAI_BASE_URL",
        "label": "OpenAI API URL",
        "description": "OpenAI-compatible API base URL.",
        "input_type": "text",
        "default": "https://api.openai.com/v1",
        "group": "OpenAI",
    },

    {
        "key": "OPENAI_API_KEY",
        "label": "OpenAI API Key",
        "description": (
            "Secret API key for OpenAI. "
            "Leave blank to keep the currently saved key."
        ),
        "input_type": "password",
        "sensitive": True,
        "default": "",
        "group": "OpenAI",
    },

    {
        "key": "OPENAI_MODEL",
        "label": "OpenAI Model",
        "description": (
            "OpenAI model name used by the application."
        ),
        "input_type": "text",
        "default": "",
        "group": "OpenAI",
    },

    {
        "key": "OPENAI_MAX_TOKENS",
        "label": "OpenAI Max Output Tokens",
        "description": (
            "Maximum output tokens allowed for an OpenAI response."
        ),
        "input_type": "number",
        "default": "2048",
        "group": "OpenAI",
    },

    {
        "key": "OPENAI_TEMPERATURE",
        "label": "OpenAI Temperature",
        "description": (
            "Generation temperature. For policy/RAG use a low value "
            "such as 0 or 0.1."
        ),
        "input_type": "number",
        "default": "0.1",
        "step": "0.1",
        "group": "OpenAI",
    },


    # =================================================================
    # GOOGLE GEMINI
    # =================================================================

    {
        "key": "GEMINI_BASE_URL",
        "label": "Gemini API URL",
        "description": "Google Gemini API base URL.",
        "input_type": "text",
        "default": "https://generativelanguage.googleapis.com/v1beta",
        "group": "Gemini",
    },

    {
        "key": "GEMINI_API_KEY",
        "label": "Gemini API Key",
        "description": (
            "Secret Google Gemini API key. "
            "Leave blank to preserve the currently saved key."
        ),
        "input_type": "password",
        "sensitive": True,
        "default": "",
        "group": "Gemini",
    },

    {
        "key": "GEMINI_MODEL",
        "label": "Gemini Model",
        "description": (
            "Gemini model used by the application."
        ),
        "input_type": "text",
        "default": "",
        "group": "Gemini",
    },

    {
        "key": "GEMINI_MAX_TOKENS",
        "label": "Gemini Max Output Tokens",
        "description": (
            "Maximum generated tokens for Gemini responses."
        ),
        "input_type": "number",
        "default": "2048",
        "group": "Gemini",
    },

    {
        "key": "GEMINI_TEMPERATURE",
        "label": "Gemini Temperature",
        "description": (
            "Gemini generation temperature."
        ),
        "input_type": "number",
        "default": "0.1",
        "step": "0.1",
        "group": "Gemini",
    },


    # =================================================================
    # ANTHROPIC CLAUDE
    # =================================================================

    {
        "key": "CLAUDE_BASE_URL",
        "label": "Claude API URL",
        "description": "Anthropic Claude API base URL.",
        "input_type": "text",
        "default": "https://api.anthropic.com/v1",
        "group": "Claude",
    },

    {
        "key": "CLAUDE_API_KEY",
        "label": "Claude API Key",
        "description": (
            "Secret Anthropic Claude API key. "
            "Leave blank to preserve the currently saved key."
        ),
        "input_type": "password",
        "sensitive": True,
        "default": "",
        "group": "Claude",
    },

    {
        "key": "CLAUDE_MODEL",
        "label": "Claude Model",
        "description": (
            "Claude model name used by the application."
        ),
        "input_type": "text",
        "default": "",
        "group": "Claude",
    },

    {
        "key": "CLAUDE_MAX_TOKENS",
        "label": "Claude Max Output Tokens",
        "description": (
            "Maximum generated tokens for Claude."
        ),
        "input_type": "number",
        "default": "2048",
        "group": "Claude",
    },

    {
        "key": "CLAUDE_TEMPERATURE",
        "label": "Claude Temperature",
        "description": (
            "Claude generation temperature."
        ),
        "input_type": "number",
        "default": "0.1",
        "step": "0.1",
        "group": "Claude",
    },


    # =================================================================
    # CURRENT POLICY ENGINE
    # =================================================================

    {
        "key": "EMBED_MODEL",
        "label": "Current Policy Embedding Model",
        "description": (
            "Embedding model used by the existing Django policy engine. "
            "Independent from POLICY_V9_EMBED_MODEL."
        ),
        "input_type": "text",
        "default": "nomic-embed-text:latest",
        "group": "Current Policy Engine",
    },

    {
        "key": "POLICY_ANSWER_MODEL",
        "label": "Current Policy Answer Model",
        "description": (
            "Answer model used by the existing Django views_policy.py engine. "
            "Independent from POLICY_V9_LLM_MODEL."
        ),
        "input_type": "ollama_select",
        "default": "aya-23-8b:q5",
        "group": "Current Policy Engine",
    },


    # =================================================================
    # RAG / CONTEXT SETTINGS
    # =================================================================

    {
        "key": "RAG_TOP_K",
        "label": "RAG Top-K",
        "description": (
            "Number of candidate chunks retrieved from Qdrant."
        ),
        "input_type": "number",
        "default": "10",
        "group": "RAG",
    },

    {
        "key": "RAG_FINAL_CONTEXT_K",
        "label": "Final Context Chunks",
        "description": (
            "Number of highest-quality retrieved chunks sent to the LLM."
        ),
        "input_type": "number",
        "default": "5",
        "group": "RAG",
    },

    {
        "key": "RAG_SCORE_THRESHOLD",
        "label": "Vector Score Threshold",
        "description": (
            "Minimum vector similarity score accepted during retrieval."
        ),
        "input_type": "number",
        "default": "0.40",
        "step": "0.01",
        "group": "RAG",
    },

    {
        "key": "RAG_CONTEXT_MAX_TOKENS",
        "label": "Maximum RAG Context Tokens",
        "description": (
            "Maximum number of tokens from retrieved policy/context "
            "sent to the LLM."
        ),
        "input_type": "number",
        "default": "6000",
        "group": "RAG",
    },

    {
        "key": "RAG_ANSWER_MAX_TOKENS",
        "label": "Maximum Answer Tokens",
        "description": (
            "Maximum output token budget for RAG-generated answers."
        ),
        "input_type": "number",
        "default": "1500",
        "group": "RAG",
    },

    # -----------------------------------------------------------------
    # RERANKER / HYBRID RETRIEVAL
    # -----------------------------------------------------------------

    {
        "key": "RERANK_URL",
        "label": "Reranker API URL",
        "description": (
            "HTTP endpoint of the local reranker service used to rerank "
            "retrieved RAG candidates."
        ),
        "input_type": "text",
        "default": "http://192.168.1.144:32112",
        "group": "RAG",
    },

    {
        "key": "RERANK_MODEL",
        "label": "Reranker Model",
        "description": (
            "Model used by the reranker service. "
            "Example: qllama/bge-reranker-v2-m3:f16."
        ),
        "input_type": "text",
        "default": "qllama/bge-reranker-v2-m3:f16",
        "group": "RAG",
    },

    {
        "key": "RERANK_CANDIDATES",
        "label": "Rerank Candidates",
        "description": (
            "Maximum number of combined retrieval candidates sent to the "
            "reranker before final selection."
        ),
        "input_type": "number",
        "default": "40",
        "group": "RAG",
    },

    {
        "key": "RERANK_TOP_N",
        "label": "Rerank Top-N",
        "description": (
            "Number of highest-ranked candidates retained after reranking."
        ),
        "input_type": "number",
        "default": "14",
        "group": "RAG",
    },

    {
        "key": "VECTOR_K",
        "label": "Vector Retrieval K",
        "description": (
            "Number of candidates retrieved from vector similarity search."
        ),
        "input_type": "number",
        "default": "40",
        "group": "RAG",
    },

    {
        "key": "LEXICAL_K",
        "label": "Lexical Retrieval K",
        "description": (
            "Number of candidates retrieved from lexical/keyword search."
        ),
        "input_type": "number",
        "default": "40",
        "group": "RAG",
    },

    {
        "key": "EVIDENCE_K",
        "label": "Evidence K",
        "description": (
            "Number of final evidence chunks selected for grounded answer "
            "generation."
        ),
        "input_type": "number",
        "default": "4",
        "group": "RAG",
    },

    # =================================================================
    # PRODUCTION V9 TEST ENGINE
    # =================================================================
    #
    # These keys are intentionally independent from the normal/current
    # policy engine. /v9 or rag_engine=production_v9 should read only
    # POLICY_V9_* settings.
    #
    {
        "key": "POLICY_V9_SCRIPT",
        "label": "V9 Script Path",
        "description": (
            "Absolute path to the V9 wrapper inside the Django runtime/container."
        ),
        "input_type": "text",
        "default": "/home/datawarehouse/rag/buro-rag-production-v9-fixed.sh",
        "group": "Production V9 Test Engine",
    },

    {
        "key": "POLICY_V9_TIMEOUT",
        "label": "V9 Request Timeout (seconds)",
        "description": (
            "Maximum wall-clock time for one V9 request."
        ),
        "input_type": "number",
        "default": "900",
        "group": "Production V9 Test Engine",
    },

    {
        "key": "POLICY_V9_REQUIRE_STAFF",
        "label": "V9 Staff-Only Access",
        "description": (
            "Restrict V9 test execution to authenticated staff users."
        ),
        "input_type": "select",
        "choices": ["true", "false"],
        "default": "true",
        "group": "Production V9 Test Engine",
    },

    {
        "key": "POLICY_V9_QDRANT_URL",
        "label": "V9 Qdrant URL",
        "description": (
            "Qdrant endpoint used only by Production V9."
        ),
        "input_type": "text",
        "default": "http://192.168.1.176:31633",
        "group": "Production V9 Test Engine",
    },

    {
        "key": "POLICY_V9_COLLECTION",
        "label": "V9 Qdrant Collection",
        "description": (
            "Optional explicit version-aware V9 collection. Leave blank for script auto-discovery."
        ),
        "input_type": "text",
        "default": "",
        "group": "Production V9 Test Engine",
    },

    {
        "key": "POLICY_V9_OLLAMA_URL",
        "label": "V9 Ollama URL",
        "description": (
            "Ollama endpoint used only by Production V9."
        ),
        "input_type": "text",
        "default": "http://192.168.1.144:31134",
        "group": "Production V9 Test Engine",
    },

    {
        "key": "POLICY_V9_EMBED_MODEL",
        "label": "V9 Embedding Model",
        "description": (
            "Embedding model used only by Production V9."
        ),
        "input_type": "text",
        "default": "bge-m3:latest",
        "group": "Production V9 Test Engine",
    },

    {
        "key": "POLICY_V9_LLM_MODEL",
        "label": "V9 LLM Model",
        "description": (
            "Generation/reasoning model used only by Production V9."
        ),
        "input_type": "ollama_select",
        "default": "deepseek-r1:14b",
        "group": "Production V9 Test Engine",
    },

    {
        "key": "POLICY_V9_QUERY_REWRITE_MODEL",
        "label": "V9 Query Understanding / Rewrite Model",
        "description": (
            "Dedicated model used only for intent, tone, scenario-fact extraction, "
            "follow-up rewriting and retrieval-query generation. It does not answer policy."
        ),
        "input_type": "ollama_select",
        "default": "qwen3.5:9b",
        "group": "Production V9 Test Engine",
    },

    {
        "key": "POLICY_V9_QUERY_REWRITE_TIMEOUT",
        "label": "V9 Query Rewrite Timeout (seconds)",
        "description": (
            "Timeout for the dedicated query-understanding/rewrite model."
        ),
        "input_type": "number",
        "default": "300",
        "group": "Production V9 Test Engine",
    },

    {
        "key": "POLICY_V9_RERANK_URL",
        "label": "V9 Reranker URL",
        "description": (
            "External reranker endpoint used only by Production V9."
        ),
        "input_type": "text",
        "default": "http://192.168.1.144:32112",
        "group": "Production V9 Test Engine",
    },

    {
        "key": "POLICY_V9_RERANK_MODEL",
        "label": "V9 Reranker Model",
        "description": (
            "External reranker model used only by Production V9."
        ),
        "input_type": "text",
        "default": "qllama/bge-reranker-v2-m3:f16",
        "group": "Production V9 Test Engine",
    },

    {
        "key": "POLICY_V9_RERANK_CANDIDATES",
        "label": "V9 Rerank Candidates",
        "description": (
            "Number of fused candidates considered before reranking."
        ),
        "input_type": "number",
        "default": "40",
        "group": "Production V9 Test Engine",
    },

    {
        "key": "POLICY_V9_RERANK_TOP_N",
        "label": "V9 Rerank Top-N",
        "description": (
            "Number of candidates retained after reranking."
        ),
        "input_type": "number",
        "default": "14",
        "group": "Production V9 Test Engine",
    },

    {
        "key": "POLICY_V9_VECTOR_K",
        "label": "V9 Vector K",
        "description": (
            "Dense/vector retrieval depth used only by Production V9."
        ),
        "input_type": "number",
        "default": "40",
        "group": "Production V9 Test Engine",
    },

    {
        "key": "POLICY_V9_LEXICAL_K",
        "label": "V9 Lexical K",
        "description": (
            "Lexical retrieval depth used only by Production V9."
        ),
        "input_type": "number",
        "default": "40",
        "group": "Production V9 Test Engine",
    },

    {
        "key": "POLICY_V9_EVIDENCE_K",
        "label": "V9 Evidence K",
        "description": (
            "Final authoritative evidence count used by Production V9."
        ),
        "input_type": "number",
        "default": "4",
        "group": "Production V9 Test Engine",
    },

    {
        "key": "POLICY_V9_HTTP_TIMEOUT",
        "label": "V9 HTTP Timeout (seconds)",
        "description": (
            "HTTP timeout exported to the V9 process."
        ),
        "input_type": "number",
        "default": "300",
        "group": "Production V9 Test Engine",
    },

    {
        "key": "POLICY_V9_LLM_TIMEOUT",
        "label": "V9 LLM Timeout (seconds)",
        "description": (
            "LLM timeout exported to the V9 process."
        ),
        "input_type": "number",
        "default": "1800",
        "group": "Production V9 Test Engine",
    },

    {
        "key": "POLICY_V9_OLLAMA_KEEP_ALIVE",
        "label": "V9 Ollama Keep Alive",
        "description": (
            "Ollama keep-alive exported to the V9 process."
        ),
        "input_type": "text",
        "default": "30m",
        "group": "Production V9 Test Engine",
    },

    {
        "key": "POLICY_V9_RERANK_REQUIRED",
        "label": "V9 Require Reranker",
        "description": (
            "If true, V9 should fail closed when the external reranker is unavailable."
        ),
        "input_type": "select",
        "choices": ["true", "false"],
        "default": "false",
        "group": "Production V9 Test Engine",
    },


    # =================================================================
    # QDRANT
    # =================================================================

    {
        "key": "QDRANT_URL",
        "label": "Main Qdrant URL",
        "description": "Default Qdrant URL used by application modules.",
        "input_type": "text",
        "default": "",
        "group": "Qdrant",
    },

    {
        "key": "QDRANT_PRIMARY_URL",
        "label": "Primary Qdrant URL",
        "description": "Primary Qdrant endpoint.",
        "input_type": "text",
        "default": "",
        "group": "Qdrant",
    },

    {
        "key": "QDRANT_SECONDARY_URL",
        "label": "Secondary Qdrant URL",
        "description": "Secondary/failover Qdrant endpoint.",
        "input_type": "text",
        "default": "",
        "group": "Qdrant",
    },


    # =================================================================
    # QDRANT COLLECTIONS
    # =================================================================

    {
        "key": "QDRANT_COLLECTION_POLICY",
        "label": "Policy Collection",
        "description": "Qdrant collection for policy RAG.",
        "input_type": "text",
        "default": "",
        "group": "Qdrant Collections",
    },

    {
        "key": "QDRANT_COLLECTION_NOTICE",
        "label": "Notice Collection",
        "description": "Qdrant collection for HR notice RAG.",
        "input_type": "text",
        "default": "",
        "group": "Qdrant Collections",
    },

    {
        "key": "QDRANT_COLLECTION_CUSTOMER_SUPPORT",
        "label": "Customer Support Collection",
        "description": (
            "Qdrant collection for customer support knowledge."
        ),
        "input_type": "text",
        "default": "",
        "group": "Qdrant Collections",
    },

    {
        "key": "QDRANT_COLLECTION_LONG_MEMORY",
        "label": "Long Memory Collection",
        "description": (
            "Qdrant collection for long-term user memory."
        ),
        "input_type": "text",
        "default": "",
        "group": "Qdrant Collections",
    },

    {
        "key": "QDRANT_COLLECTION_CASHFLOW",
        "label": "Cashflow Collection",
        "description": "Qdrant collection for cashflow RAG.",
        "input_type": "text",
        "default": "",
        "group": "Qdrant Collections",
    },


    # =================================================================
    # NIFI
    # =================================================================

    {
        "key": "NIFI_BASE_URL",
        "label": "NiFi Base URL",
        "description": "Base NiFi endpoint.",
        "input_type": "text",
        "default": "",
        "group": "NiFi",
    },

    {
        "key": "NIFI_CUSTOMER_SUPPORT_URL",
        "label": "NiFi Customer Support URL",
        "description": (
            "NiFi endpoint for the customer support module."
        ),
        "input_type": "text",
        "default": "",
        "group": "NiFi",
    },

    {
        "key": "NIFI_HR_URL",
        "label": "NiFi HR URL",
        "description": "NiFi endpoint for the HR module.",
        "input_type": "text",
        "default": "",
        "group": "NiFi",
    },

    {
        "key": "NIFI_CORE_URL",
        "label": "NiFi Core URL",
        "description": "NiFi endpoint for the core module.",
        "input_type": "text",
        "default": "",
        "group": "NiFi",
    },

    {
        "key": "NIFI_CASHFLOW_URL",
        "label": "NiFi Cashflow URL",
        "description": "NiFi endpoint for the cashflow module.",
        "input_type": "text",
        "default": "",
        "group": "NiFi",
    },
]



# MCP uses the same project model; only gateway access needs configuration.
MANAGED_SETTINGS.extend([
    {
        "key": "MCP_GATEWAY_ENABLED", "label": "Enable MCP tool selection",
        "description": "Set true to let the project model select role-permitted gateway tools; false uses the local router.",
        "input_type": "select", "choices": ["false", "true"], "default": "false", "group": "MCP Gateway",
    },
    {
        "key": "MCP_GATEWAY_URL", "label": "MCP gateway endpoint",
        "description": "Full Streamable HTTP MCP endpoint. Verify whether your server uses /mcp.",
        "input_type": "text", "default": "http://192.168.1.99:30444/mcp", "group": "MCP Gateway",
    },
    {
        "key": "MCP_REQUEST_TIMEOUT", "label": "MCP request deadline (seconds)",
        "description": "Overall discovery, model selection and execution deadline. Keep below the web worker timeout.",
        "input_type": "number", "default": "90", "group": "MCP Gateway",
    },
    {
        "key": "MCP_TIMEOUT", "label": "MCP operation timeout (seconds)",
        "description": "Timeout for each network operation; the overall request deadline still applies.",
        "input_type": "number", "default": "30", "group": "MCP Gateway",
    },
    {
        "key": "MCP_GATEWAY_TOKEN", "label": "MCP gateway bearer token",
        "description": "Optional gateway credential. Leave blank to keep the saved value.",
        "input_type": "password", "sensitive": True, "default": "", "group": "MCP Gateway",
    },
])

# --- OPENAI_POLICY_MANAGED_SETTINGS_BEGIN ---
# Reuse the existing settings dashboard. Existing OPENAI_API_KEY and
# OPENAI_MODEL entries are not duplicated.
_OPENAI_POLICY_SETTINGS = [
    {
        "key": "OPENAI_VECTOR_STORE_ID",
        "label": "Policy Vector Store ID",
        "description": (
            "OpenAI Vector Store used by Organization Policy AI File Search. "
            "Example: vs_..."
        ),
        "input_type": "text",
        "default": "",
        "group": "OpenAI",
    },
    {
        "key": "OPENAI_PREPROCESS_MODEL",
        "label": "Policy Preprocess Model",
        "description": (
            "OpenAI model used for lossless Unicode/Bangla policy normalization. "
            "Leave blank to use OPENAI_MODEL."
        ),
        "input_type": "text",
        "default": "",
        "group": "OpenAI",
    },
    {
        "key": "FILE_SEARCH_MAX_RESULTS",
        "label": "Policy File Search Max Results",
        "description": "Maximum File Search results sent to the Policy AI response.",
        "input_type": "number",
        "default": "8",
        "group": "OpenAI",
    },
    {
        "key": "ADMIN_MAX_UPLOAD_MB",
        "label": "Policy Direct Upload Limit (MB)",
        "description": "Maximum upload size for direct vector-store policy uploads.",
        "input_type": "number",
        "default": "50",
        "group": "OpenAI",
    },
    {
        "key": "PREPROCESS_MAX_UPLOAD_MB",
        "label": "Policy Preprocess Upload Limit (MB)",
        "description": "Maximum source document size accepted by Policy RAG Studio preprocessing.",
        "input_type": "number",
        "default": "50",
        "group": "OpenAI",
    },
    {
        "key": "PREPROCESS_BATCH_CHARS",
        "label": "Policy Preprocess Batch Characters",
        "description": "Approximate character size of each OpenAI normalization batch.",
        "input_type": "number",
        "default": "12000",
        "group": "OpenAI",
    },
    {
        "key": "GOOGLE_CLOUD_REGION",
        "label": "Google TTS Region",
        "description": "Google Cloud Text-to-Speech region. Usually global.",
        "input_type": "text",
        "default": "global",
        "group": "OpenAI",
    },
    {
        "key": "GOOGLE_TTS_MODEL",
        "label": "Google TTS Model",
        "description": "Google TTS model used by the optional Policy AI TTS endpoint.",
        "input_type": "text",
        "default": "gemini-2.5-flash-tts",
        "group": "OpenAI",
    },
    {
        "key": "GOOGLE_TTS_VOICE",
        "label": "Google TTS Voice",
        "description": "Voice name used by the optional Policy AI TTS endpoint.",
        "input_type": "text",
        "default": "Kore",
        "group": "OpenAI",
    },
]

_existing_openai_policy_keys = {item["key"] for item in MANAGED_SETTINGS}
MANAGED_SETTINGS.extend(
    item
    for item in _OPENAI_POLICY_SETTINGS
    if item["key"] not in _existing_openai_policy_keys
)
# --- OPENAI_POLICY_MANAGED_SETTINGS_END ---

# ---------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------

def _get_fallback_value(item):
    """
    Determine fallback value.

    Priority:
        dynamic_config.DEFAULT_SETTINGS
        -> MANAGED_SETTINGS default
        -> empty string
    """

    key = item["key"]

    if key in DEFAULT_SETTINGS:
        default_value = DEFAULT_SETTINGS.get(key, "")
    else:
        default_value = item.get("default", "")

    return get_setting(key, default_value)


def _prepare_ollama_choices(current_value, ollama_models):
    """
    Return Ollama model choices while preserving a model stored in DB
    even if Ollama cannot currently report it.
    """

    choices = list(ollama_models or [])

    if current_value and current_value not in choices:
        choices.insert(0, current_value)

    return choices


# ---------------------------------------------------------------------
# SETTINGS DASHBOARD
# ---------------------------------------------------------------------

@root_settings_user_required
@csrf_protect
def settings_dashboard(request):

    managed_keys = [item["key"] for item in MANAGED_SETTINGS]

    # -----------------------------------------------------------------
    # SAVE SETTINGS
    # -----------------------------------------------------------------

    if request.method == "POST":

        existing_settings = {
            obj.key: obj
            for obj in SystemSetting.objects.filter(key__in=managed_keys)
        }

        with transaction.atomic():

            for item in MANAGED_SETTINGS:

                key = item["key"]
                sensitive = item.get("sensitive", False)

                submitted_value = request.POST.get(key)

                if submitted_value is None:
                    submitted_value = ""

                value = submitted_value.strip()

                # -----------------------------------------------------
                # SECRET HANDLING
                #
                # Empty API key field means:
                # "keep the existing secret"
                # -----------------------------------------------------

                if sensitive and not value:

                    existing_obj = existing_settings.get(key)

                    if existing_obj and existing_obj.value:
                        continue

                SystemSetting.objects.update_or_create(
                    key=key,
                    defaults={
                        "value": value,
                        "description": item.get("description", ""),
                        "is_active": True,
                    },
                )

            # ---------------------------------------------------------
            # BACKWARD COMPATIBILITY
            #
            # Some old modules still use GEN_MODEL.
            # Keep it synchronized with OLLAMA_MODEL.
            # ---------------------------------------------------------

            ollama_model = (
                request.POST.get("OLLAMA_MODEL") or ""
            ).strip()

            if ollama_model:

                SystemSetting.objects.update_or_create(
                    key="GEN_MODEL",
                    defaults={
                        "value": ollama_model,
                        "description": (
                            "Backward-compatible alias "
                            "for OLLAMA_MODEL."
                        ),
                        "is_active": True,
                    },
                )

        clear_setting_cache()

        messages.success(
            request,
            "AI and system settings updated successfully.",
        )

        return redirect("admin_tools_settings")

    # -----------------------------------------------------------------
    # LOAD SETTINGS
    # -----------------------------------------------------------------

    rows = []

    db_settings = {
        obj.key: obj
        for obj in SystemSetting.objects.filter(
            key__in=managed_keys
        )
    }

    # Do not allow failure of Ollama model discovery
    # to break the entire settings page.
    try:
        ollama_models = get_available_ollama_models() or []
    except Exception:
        ollama_models = []

    for item in MANAGED_SETTINGS:

        key = item["key"]
        db_obj = db_settings.get(key)

        if db_obj and db_obj.value:
            source = "Database"
            actual_value = db_obj.value
        else:
            source = "Fallback"
            actual_value = _get_fallback_value(item)

        sensitive = item.get("sensitive", False)

        # -------------------------------------------------------------
        # IMPORTANT:
        # Never send the actual API secret back into the HTML page.
        # -------------------------------------------------------------

        if sensitive:
            display_value = ""
            has_saved_value = bool(actual_value)

        else:
            display_value = actual_value
            has_saved_value = False

        # -------------------------------------------------------------
        # SELECT CHOICES
        # -------------------------------------------------------------

        input_type = item.get("input_type", "text")
        choices = []

        if input_type == "ollama_select":

            choices = _prepare_ollama_choices(
                actual_value,
                ollama_models,
            )

            # HTML template can treat this as a normal select.
            rendered_input_type = "select"

        elif input_type == "select":

            choices = list(item.get("choices", []))

            if actual_value and actual_value not in choices:
                choices.insert(0, actual_value)

            rendered_input_type = "select"

        else:
            rendered_input_type = input_type

        rows.append(
            {
                "key": key,
                "label": item["label"],
                "description": item.get("description", ""),
                "value": display_value,
                "source": source,
                "input_type": rendered_input_type,
                "choices": choices,
                "group": item.get("group", "General"),
                "sensitive": sensitive,
                "has_saved_value": has_saved_value,
                "step": item.get("step"),
            }
        )

    # -----------------------------------------------------------------
    # BUILD GROUPS
    #
    # This is optional for your template. Existing templates can
    # continue using "rows". A newer UI can use "grouped_rows".
    # -----------------------------------------------------------------

    grouped_rows = {}

    for row in rows:

        group_name = row.get("group", "General")

        grouped_rows.setdefault(
            group_name,
            []
        ).append(row)

    return render(
        request,
        "admin/settings_dashboard.html",
        {
            "rows": rows,
            "grouped_rows": grouped_rows,
            "ollama_models": ollama_models,
        },
    )

# --- DEEPSEEK_SETTINGS_UI_BEGIN ---
# Cloud DeepSeek API settings. These are separate from local Ollama DeepSeek models.
_existing_deepseek_keys = {item.get("key") for item in MANAGED_SETTINGS}

_deepseek_settings = [
    {
        "key": "DEEPSEEK_BASE_URL",
        "label": "DeepSeek API URL",
        "description": "DeepSeek cloud API base URL. Default: https://api.deepseek.com",
        "input_type": "text",
        "default": "https://api.deepseek.com",
        "group": "DeepSeek",
    },
    {
        "key": "DEEPSEEK_API_KEY",
        "label": "DeepSeek API Key",
        "description": "Secret API key for DeepSeek. Leave blank to keep the currently saved key.",
        "input_type": "password",
        "default": "",
        "group": "DeepSeek",
        "sensitive": True,
    },
    {
        "key": "DEEPSEEK_MODEL",
        "label": "DeepSeek Model",
        "description": "DeepSeek cloud model name. Example: deepseek-v4-flash or deepseek-v4-pro.",
        "input_type": "text",
        "default": "deepseek-v4-flash",
        "group": "DeepSeek",
    },
    {
        "key": "DEEPSEEK_MAX_TOKENS",
        "label": "DeepSeek Max Output Tokens",
        "description": "Maximum output tokens allowed for a DeepSeek response.",
        "input_type": "number",
        "default": "2048",
        "group": "DeepSeek",
    },
    {
        "key": "DEEPSEEK_TEMPERATURE",
        "label": "DeepSeek Temperature",
        "description": "Generation temperature for DeepSeek. For policy/RAG use a low value such as 0 or 0.1.",
        "input_type": "number",
        "default": "0.1",
        "group": "DeepSeek",
    },
]

MANAGED_SETTINGS.extend(
    item for item in _deepseek_settings
    if item["key"] not in _existing_deepseek_keys
)
# --- DEEPSEEK_SETTINGS_UI_END ---
