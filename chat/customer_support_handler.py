import json
import os
import re
import requests

from django.contrib.auth.models import AnonymousUser
from django.http import JsonResponse

from .models import CustomerSupportTicket
from .customer_support_router import extract_ticket_id
from .customer_support_nifi import (
    call_customer_support_nifi,
    build_customer_support_nifi_payload,
)
from .customer_support_retrieval import (
    search_customer_support_knowledge,
    build_context_from_docs,
)


CUSTOMER_GROUP = "customer_support_customer"
BRANCH_GROUP = "customer_support_branch"
AREA_GROUP = "customer_support_area"
HEAD_OFFICE_GROUP = "customer_support_head_office"
ADMIN_GROUP = "customer_support_admin"

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://192.168.1.144:31134").rstrip("/")
OLLAMA_MODEL = os.getenv("GEN_MODEL", "aya-23-8b:q5")
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "120"))


def get_user_groups(user):
    if not user or isinstance(user, AnonymousUser) or not user.is_authenticated:
        return set()

    return set(user.groups.values_list("name", flat=True))


def get_customer_role(user):
    groups = get_user_groups(user)

    if user and user.is_authenticated and user.is_superuser:
        return "customer_support_admin"

    if ADMIN_GROUP in groups:
        return ADMIN_GROUP

    if HEAD_OFFICE_GROUP in groups:
        return HEAD_OFFICE_GROUP

    if AREA_GROUP in groups:
        return AREA_GROUP

    if BRANCH_GROUP in groups:
        return BRANCH_GROUP

    if CUSTOMER_GROUP in groups:
        return CUSTOMER_GROUP

    return "public"


def has_any_group(user, groups):
    if user and user.is_authenticated and user.is_superuser:
        return True

    return bool(get_user_groups(user).intersection(set(groups)))


def safe_response(intent, answer, extra=None, allowed=True, status="success"):
    response = {
        "module": "customer_support",
        "intent": intent,
        "allowed": allowed,
        "status": status,
        "answer": answer,
    }

    if extra:
        response.update(extra)

    return response


def create_local_ticket(user, data, complaint_type):
    ticket = CustomerSupportTicket.objects.create(
        member_id=data.get("member_id") or "",
        mobile_no=data.get("mobile_no") or "",
        branch_code=data.get("branch_code") or "",
        complaint_type=complaint_type,
        complaint_text=data.get("message") or data.get("complaint_text") or "",
        created_by=user if user and user.is_authenticated else None,
        status="OPEN",
        priority=data.get("priority") or "NORMAL",
        escalation_level="BRANCH",
        source="DJANGO_LOCAL",
    )

    return ticket


def extract_loaneeno_from_question(question):
    text = question or ""
    match = re.search(r"LOANEENO[^0-9]*([0-9]+)", text, re.IGNORECASE)

    if match:
        return match.group(1)

    return ""


def extract_branchcode_from_question(question):
    text = question or ""

    match = re.search(
        r"(?:branch\s*code|branchcode|branch|BRANCHCODE)[^0-9]*([0-9]{4})",
        text,
        re.IGNORECASE,
    )

    if match:
        return match.group(1)

    match = re.search(r"শাখা[^0-9]*([0-9]{4})", text)

    if match:
        return match.group(1)

    return ""


def is_loanee_list_by_branch_query(question):
    q = (question or "").lower()
    original = question or ""

    english_match = (
        ("loanee" in q or "loaneeno" in q or "loan no" in q or "loan number" in q)
        and ("branch" in q or "branch code" in q or "branchcode" in q)
        and ("list" in q or "show" in q or "all" in q)
    )

    bangla_match = (
        ("শাখা" in original)
        and (
            "loaneeno" in q
            or "loanee" in q
            or "ঋণ নম্বর" in original
            or "লোন নম্বর" in original
        )
        and (
            "তালিকা" in original
            or "দেখাও" in original
            or "দেখান" in original
            or "সকল" in original
            or "সব" in original
        )
    )

    return english_match or bangla_match


def format_loanee_list_by_branch_answer(question, data):
    branch_code = extract_branchcode_from_question(question)

    if not data:
        if branch_code:
            return f"Branch code {branch_code} এর জন্য কোনো LOANEENO পাওয়া যায়নি।"
        return "এই branch এর জন্য কোনো LOANEENO পাওয়া যায়নি।"

    lines = []

    if branch_code:
        lines.append(f"Branch code {branch_code} এর LOANEENO তালিকা পাওয়া গেছে।")
    else:
        lines.append("LOANEENO তালিকা পাওয়া গেছে।")

    lines.append(f"মোট দেখানো হয়েছে: {len(data)} টি record।")
    lines.append("")

    for idx, row in enumerate(data[:100], start=1):
        loaneeno = row.get("LOANEENO", "")
        branchcode = row.get("BRANCHCODE", "")
        samitycode = row.get("SAMITYCODE", "")
        reportingmonth = row.get("REPORTINGMONTH", "")

        parts = [f"{idx}. LOANEENO: {loaneeno}"]

        if branchcode:
            parts.append(f"Branch: {branchcode}")

        if samitycode:
            parts.append(f"Samity: {samitycode}")

        if reportingmonth:
            parts.append(f"Reporting Month: {reportingmonth}")

        lines.append(", ".join(parts))

    return "\n".join(lines)


def fallback_loan_summary_answer(data):
    if not data:
        return "এই LOANEENO এর জন্য কোনো loan summary পাওয়া যায়নি।"

    row = data[0]

    loaneeno = row.get("LOANEENO", "")
    reporting_month = row.get("REPORTINGMONTH", "")
    branch_code = row.get("BRANCHCODE", "")
    samity_code = row.get("SAMITYCODE", "")

    disburse_amount = row.get("DISBURSEAMOUNT", "")
    disburse_date = row.get("DISBURSEDATE", "")
    maturity_date = row.get("MATURITYDATE", "")

    realizable = row.get("REALIZABLE", "")
    realized = row.get("REALIZED", "")
    due_inst_no = row.get("DUEINSTNO", "")
    due_amount = row.get("DUEAMOUNT", "")
    outstanding = row.get("OUTSTANDING", "")
    outstanding_p = row.get("OUTSTANDINGP", "")

    return (
        f"এই LOANEENO {loaneeno} এর সর্বশেষ loan summary পাওয়া গেছে।\n\n"
        f"Reporting Month: {reporting_month}\n"
        f"Branch Code: {branch_code}\n"
        f"Samity Code: {samity_code}\n\n"
        f"Disburse Amount: {disburse_amount} টাকা\n"
        f"Disburse Date: {disburse_date}\n"
        f"Maturity Date: {maturity_date}\n\n"
        f"Realizable Amount: {realizable} টাকা\n"
        f"Realized Amount: {realized} টাকা\n"
        f"Due Installment No: {due_inst_no}\n"
        f"Due Amount: {due_amount} টাকা\n"
        f"Outstanding Amount: {outstanding} টাকা\n"
        f"Outstanding Principal: {outstanding_p} টাকা\n\n"
        f"সংক্ষেপে, সর্বশেষ reporting month {reporting_month} অনুযায়ী "
        f"এই loan account-এ outstanding amount {outstanding} টাকা এবং "
        f"due amount {due_amount} টাকা আছে।"
    )


def explain_loan_summary_with_llm(question, data):
    if not data:
        return "এই LOANEENO এর জন্য কোনো loan summary পাওয়া যায়নি।"

    prompt = f"""
You are a customer support assistant for a microfinance/loan system.

User question:
{question}

Oracle/NiFi returned this loan summary data:
{json.dumps(data, ensure_ascii=False, indent=2)}

Task:
Explain the loan summary in simple Bangla.

Rules:
- Use only the provided data.
- Do not invent anything.
- Mention REPORTINGMONTH.
- Explain DISBURSEAMOUNT, REALIZED, DUEAMOUNT, OUTSTANDING, OUTSTANDINGP, DISBURSEDATE, MATURITYDATE.
- Keep the explanation short and customer-friendly.
- Use Bangla, but keep important field names in English brackets.
"""

    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {
                "role": "system",
                "content": "You explain customer loan summary data clearly and accurately.",
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
        "stream": False,
        "options": {
            "temperature": 0.2,
            "top_p": 0.85,
            "num_ctx": 2048,
            "num_predict": 180,
        },
    }

    try:
        response = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json=payload,
            timeout=OLLAMA_TIMEOUT,
        )
        response.raise_for_status()
        result = response.json()

        answer = (
            result.get("message", {}).get("content")
            or result.get("response")
            or ""
        ).strip()

        if answer:
            return answer

        return fallback_loan_summary_answer(data)

    except Exception:
        return fallback_loan_summary_answer(data)


def is_live_customer_data_query(question):
    q = (question or "").lower()
    original = question or ""

    live_keywords = [
        "loaneeno",
        "loan summary",
        "loan status",
        "loan details",
        "outstanding",
        "due amount",
        "savings balance",
        "ticket status",
        "member id",
        "member no",
        "mobile no",
        "mobile number",
        "account no",
        "account number",

        # LOANEENO list / branch-wise list
        "show all loaneeno",
        "list all loaneeno",
        "all loaneeno",
        "loaneeno list",
        "list loanee",
        "loanee list",
        "list loanee no",
        "show loanee no",
        "show loanee list",
        "loanee no by branch",
        "loaneeno by branch",
        "list loaneeno by branch",
        "show all loaneeno for branch",
        "show loanee no for branch",
        "show loanee list for branch",
        "branch wise loaneeno",
        "branchwise loaneeno",
        "branch loanee list",
        "branch loaneeno list",
        "branch code",
        "branchcode",
    ]

    bangla_live_keywords = [
        "ঋণের সারাংশ",
        "লোন সামারি",
        "লোন স্ট্যাটাস",
        "বকেয়া কত",
        "বকেয়া টাকা",
        "সঞ্চয় ব্যালেন্স",
        "টিকিট স্ট্যাটাস",
        "loaneeno তালিকা",
        "loanee তালিকা",
        "ঋণ নম্বর তালিকা",
        "লোন নম্বর তালিকা",
        "শাখা অনুযায়ী loaneeno",
        "শাখা অনুযায়ী ঋণ নম্বর",
        "শাখার loaneeno",
        "শাখার ঋণ নম্বর",
        "শাখাভিত্তিক loaneeno",
        "শাখাভিত্তিক ঋণ নম্বর",
    ]

    if extract_loaneeno_from_question(question):
        return True

    if is_loanee_list_by_branch_query(question):
        return True

    return any(k in q for k in live_keywords) or any(k in original for k in bangla_live_keywords)


def detect_rag_intent(question):
    q = (question or "").lower()
    original = question or ""

    if any(k in q for k in ["apply loan", "loan application", "new loan"]):
        return "loan_application"

    if any(k in original for k in ["ঋণ নিতে", "লোন নিতে", "ঋণের আবেদন", "নতুন ঋণ"]):
        return "loan_application"

    if any(k in q for k in ["eligible", "eligibility", "qualified", "qualification"]):
        return "loan_eligibility"

    if any(k in original for k in ["যোগ্যতা", "ঋণ পাওয়ার যোগ্য", "লোন পাওয়ার যোগ্য"]):
        return "loan_eligibility"

    if any(k in q for k in ["installment", "payment method", "repayment", "pay installment"]):
        return "payment_method_info"

    if any(k in original for k in ["কিস্তি জমা", "কিস্তি দেওয়ার", "কিস্তি পরিশোধ", "পেমেন্ট"]):
        return "payment_method_info"

    if any(k in q for k in ["close account", "account closing", "account closure"]):
        return "account_closing"

    if any(k in original for k in ["হিসাব বন্ধ", "অ্যাকাউন্ট বন্ধ", "একাউন্ট বন্ধ"]):
        return "account_closing"

    if any(k in q for k in ["required document", "documents", "nid", "guarantor", "nominee"]):
        return "required_documents"

    if any(k in original for k in ["কাগজপত্র", "কাগজ", "ডকুমেন্ট", "এনআইডি", "জামিনদার", "নমিনি"]):
        return "required_documents"

    if any(k in original for k in ["নতুন সদস্য", "সদস্য হতে", "সদস্য ভর্তি", "মেম্বার হতে"]):
        return "required_documents"

    if any(k in q for k in ["branch", "branch contact", "field officer"]):
        return "branch_info"

    if any(k in original for k in ["শাখা", "মাঠকর্মী", "যোগাযোগ"]):
        return "branch_info"

    if any(k in q for k in ["complaint", "ticket", "support"]):
        return "ticket_create"

    if any(k in original for k in ["অভিযোগ", "টিকিট", "সমস্যা"]):
        return "ticket_create"

    return "general_faq"


def fallback_rag_answer_from_docs(docs):
    if not docs:
        return "এই বিষয়ে customer support knowledge base-এ কোনো তথ্য পাওয়া যায়নি। অনুগ্রহ করে শাখায় যোগাযোগ করুন।"

    first_doc = docs[0]
    text = first_doc.get("text") or ""

    if text:
        return text

    topic_bn = first_doc.get("topic_bn") or ""
    topic_en = first_doc.get("topic_en") or ""

    return (
        f"{topic_bn or topic_en} বিষয়ে knowledge পাওয়া গেছে, "
        f"কিন্তু বিস্তারিত উত্তর তৈরি করা যায়নি। অনুগ্রহ করে সংশ্লিষ্ট শাখায় যোগাযোগ করুন।"
    )


def answer_customer_support_rag(question):
    intent = detect_rag_intent(question)

    docs = search_customer_support_knowledge(
        question=question,
        intent=intent,
        limit=1,
    )

    context = build_context_from_docs(docs)

    if not context:
        return {
            "intent": intent,
            "answer": "এই বিষয়ে customer support knowledge base-এ কোনো তথ্য পাওয়া যায়নি। অনুগ্রহ করে শাখায় যোগাযোগ করুন।",
            "rag_docs": [],
            "llm_used": False,
        }

    prompt = f"""
You are a customer support assistant for a microfinance organization.

User question:
{question}

Retrieved knowledge base context:
{context}

Task:
Answer the user's question in simple Bangla.

Rules:
- Use only the provided context.
- Do not invent financial/account data.
- If branch verification is needed, say that clearly.
- Keep answer short, helpful, and customer-friendly.
"""

    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {
                "role": "system",
                "content": "You answer customer support FAQ questions using retrieved knowledge only.",
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
        "stream": False,
        "options": {
            "temperature": 0.2,
            "top_p": 0.85,
            "num_ctx": 2048,
            "num_predict": 180,
        },
    }

    try:
        response = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json=payload,
            timeout=OLLAMA_TIMEOUT,
        )
        response.raise_for_status()
        result = response.json()

        answer = (
            result.get("message", {}).get("content")
            or result.get("response")
            or ""
        ).strip()

        if not answer:
            answer = fallback_rag_answer_from_docs(docs)

        return {
            "intent": intent,
            "answer": answer,
            "rag_docs": docs,
            "llm_used": True,
        }

    except Exception as e:
        return {
            "intent": intent,
            "answer": fallback_rag_answer_from_docs(docs),
            "rag_docs": docs,
            "llm_used": False,
            "llm_error": str(e),
        }


def handle_customer_verify(user, data):
    member_id = data.get("member_id") or ""
    mobile_no = data.get("mobile_no") or ""

    if not member_id and not mobile_no:
        return safe_response(
            "customer_verify",
            "তথ্য যাচাইয়ের জন্য member_id অথবা mobile_no প্রয়োজন।",
            {
                "verified": False,
                "verification_mode": "MISSING_REQUIRED_FIELD",
            },
            status="missing_required_field",
        )

    nifi_payload = build_customer_support_nifi_payload(
        user=user,
        data=data,
        intent="customer_verify",
    )

    nifi_result = call_customer_support_nifi(nifi_payload)

    return safe_response(
        "customer_verify",
        "Customer verification request has been sent to NiFi.",
        {
            "verified": True,
            "member_id": member_id,
            "mobile_no": mobile_no,
            "verification_source": "NIFI",
            "nifi_payload": nifi_payload,
            "nifi_result": nifi_result,
        },
    )


def handle_installment_problem(user, data):
    role = get_customer_role(user)

    if role == "public":
        return safe_response(
            "installment_problem",
            "কিস্তি আপডেট সংক্রান্ত তথ্য জানতে হলে আপনাকে customer/support user হিসেবে login করতে হবে।",
            {"next_action": "login_required"},
            allowed=False,
            status="login_required",
        )

    ticket = create_local_ticket(user, data, "installment_problem")

    nifi_payload = build_customer_support_nifi_payload(
        user=user,
        data=data,
        intent="installment_problem",
        ticket=ticket,
    )

    nifi_result = call_customer_support_nifi(nifi_payload)

    return safe_response(
        "installment_problem",
        f"আপনার কিস্তি আপডেট সংক্রান্ত অভিযোগ গ্রহণ করা হয়েছে। আপনার টিকিট নম্বর #{ticket.id}।",
        {
            "ticket_id": ticket.id,
            "ticket_status": ticket.status,
            "verification_source": "DJANGO_LOCAL_AND_NIFI",
            "nifi_payload": nifi_payload,
            "nifi_result": nifi_result,
        },
    )


def handle_ticket_create(user, data):
    role = get_customer_role(user)

    if role == "public":
        return safe_response(
            "ticket_create",
            "অভিযোগ জমা দিতে হলে login প্রয়োজন। অনুগ্রহ করে customer account দিয়ে login করুন।",
            allowed=False,
            status="login_required",
        )

    complaint_type = data.get("complaint_type") or data.get("intent") or "general_support"

    ticket = create_local_ticket(
        user=user,
        data=data,
        complaint_type=complaint_type,
    )

    nifi_payload = build_customer_support_nifi_payload(
        user=user,
        data=data,
        intent="ticket_create",
        ticket=ticket,
    )

    nifi_result = call_customer_support_nifi(nifi_payload)

    return safe_response(
        "ticket_create",
        f"আপনার অভিযোগ সফলভাবে গ্রহণ করা হয়েছে। আপনার টিকিট নম্বর #{ticket.id}।",
        {
            "ticket_id": ticket.id,
            "ticket_status": ticket.status,
            "complaint_type": ticket.complaint_type,
            "verification_source": "DJANGO_LOCAL_AND_NIFI",
            "nifi_payload": nifi_payload,
            "nifi_result": nifi_result,
        },
    )


def handle_ticket_status(user, data):
    ticket_id = data.get("ticket_id") or extract_ticket_id(data.get("message", ""))

    if not ticket_id:
        return safe_response(
            "ticket_status",
            "টিকিটের অবস্থা জানতে ticket_id দিন। উদাহরণ: ticket status 1",
            status="missing_ticket_id",
        )

    try:
        ticket = CustomerSupportTicket.objects.get(id=ticket_id)
    except CustomerSupportTicket.DoesNotExist:
        return safe_response(
            "ticket_status",
            "এই টিকিট নম্বর পাওয়া যায়নি। অনুগ্রহ করে সঠিক ticket_id দিন।",
            {"ticket_id": ticket_id},
            status="not_found",
        )

    if not ticket.can_be_seen_by(user):
        return safe_response(
            "ticket_status",
            "এই টিকিট দেখার অনুমতি আপনার নেই।",
            {"ticket_id": ticket_id},
            allowed=False,
            status="forbidden",
        )

    nifi_payload = build_customer_support_nifi_payload(
        user=user,
        data=data,
        intent="ticket_status",
        ticket=ticket,
    )

    nifi_result = call_customer_support_nifi(nifi_payload)

    return safe_response(
        "ticket_status",
        f"আপনার টিকিট #{ticket.id} এর বর্তমান অবস্থা: {ticket.status}. Escalation level: {ticket.escalation_level}.",
        {
            "ticket_id": ticket.id,
            "ticket_status": ticket.status,
            "priority": ticket.priority,
            "escalation_level": ticket.escalation_level,
            "resolution_note": ticket.resolution_note or "",
            "created_at": ticket.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            "updated_at": ticket.updated_at.strftime("%Y-%m-%d %H:%M:%S"),
            "verification_source": "DJANGO_LOCAL_AND_NIFI",
            "nifi_payload": nifi_payload,
            "nifi_result": nifi_result,
        },
    )


def handle_branch_info(user, data):
    nifi_payload = build_customer_support_nifi_payload(
        user=user,
        data=data,
        intent="branch_info",
    )

    nifi_result = call_customer_support_nifi(nifi_payload)

    return safe_response(
        "branch_info",
        "শাখার তথ্য NiFi থেকে যাচাই করা হচ্ছে।",
        {
            "branch_code": data.get("branch_code") or "",
            "verification_source": "NIFI",
            "nifi_payload": nifi_payload,
            "nifi_result": nifi_result,
        },
    )


def handle_loan_summary(user, data):
    role = get_customer_role(user)

    if role == "public":
        return safe_response(
            "loan_summary",
            "ঋণ সংক্রান্ত তথ্য দেখতে customer verification প্রয়োজন।",
            allowed=False,
            status="verification_required",
        )

    nifi_payload = build_customer_support_nifi_payload(
        user=user,
        data=data,
        intent="loan_summary",
    )

    nifi_result = call_customer_support_nifi(nifi_payload)

    if isinstance(nifi_result, dict) and nifi_result.get("success"):
        loan_data = nifi_result.get("data", [])
        answer = explain_loan_summary_with_llm(
            data.get("message") or data.get("question") or "",
            loan_data,
        )

        return safe_response(
            "loan_summary",
            answer,
            {
                "verification_source": "NIFI",
                "nifi_payload": nifi_payload,
                "nifi_result": nifi_result,
                "data": loan_data,
            },
        )

    return safe_response(
        "loan_summary",
        "Loan summary request has been sent to NiFi, but no successful data was returned.",
        {
            "verification_source": "NIFI",
            "future_api": "/customer/query",
            "nifi_payload": nifi_payload,
            "nifi_result": nifi_result,
        },
        status="nifi_failed",
    )


def handle_savings_summary(user, data):
    role = get_customer_role(user)

    if role == "public":
        return safe_response(
            "savings_summary",
            "সঞ্চয় সংক্রান্ত তথ্য দেখতে customer verification প্রয়োজন।",
            allowed=False,
            status="verification_required",
        )

    nifi_payload = build_customer_support_nifi_payload(
        user=user,
        data=data,
        intent="savings_summary",
    )

    nifi_result = call_customer_support_nifi(nifi_payload)

    return safe_response(
        "savings_summary",
        "Savings summary request has been sent to NiFi.",
        {
            "verification_source": "NIFI",
            "future_api": "/customer/query",
            "nifi_payload": nifi_payload,
            "nifi_result": nifi_result,
        },
    )


def handle_escalate_ticket(user, data):
    if not has_any_group(user, [BRANCH_GROUP, AREA_GROUP, HEAD_OFFICE_GROUP, ADMIN_GROUP]):
        return safe_response(
            "escalate_ticket",
            "টিকিট escalate করার অনুমতি আপনার নেই।",
            allowed=False,
            status="forbidden",
        )

    ticket_id = data.get("ticket_id") or extract_ticket_id(data.get("message", ""))

    if not ticket_id:
        return safe_response(
            "escalate_ticket",
            "Escalate করার জন্য ticket_id প্রয়োজন।",
            status="missing_ticket_id",
        )

    try:
        ticket = CustomerSupportTicket.objects.get(id=ticket_id)
    except CustomerSupportTicket.DoesNotExist:
        return safe_response(
            "escalate_ticket",
            "এই টিকিট নম্বর পাওয়া যায়নি।",
            {"ticket_id": ticket_id},
            status="not_found",
        )

    new_level = data.get("escalation_level") or "AREA"

    ticket.status = "ESCALATED"
    ticket.escalation_level = new_level
    ticket.resolution_note = data.get("note") or data.get("message") or "Ticket escalated."
    ticket.save()

    nifi_payload = build_customer_support_nifi_payload(
        user=user,
        data=data,
        intent="escalate_ticket",
        ticket=ticket,
    )

    nifi_result = call_customer_support_nifi(nifi_payload)

    return safe_response(
        "escalate_ticket",
        f"টিকিট #{ticket.id} সফলভাবে {new_level} পর্যায়ে escalate করা হয়েছে।",
        {
            "ticket_id": ticket.id,
            "ticket_status": ticket.status,
            "escalation_level": ticket.escalation_level,
            "verification_source": "DJANGO_LOCAL_AND_NIFI",
            "nifi_payload": nifi_payload,
            "nifi_result": nifi_result,
        },
    )


def handle_general_support(user, data):
    return safe_response(
        "general_support",
        "আমি BURO customer support assistant। আপনি কিস্তি সমস্যা, অভিযোগ, টিকিট স্ট্যাটাস, শাখার তথ্য, ঋণ বা সঞ্চয় সংক্রান্ত প্রশ্ন করতে পারেন।",
        {
            "sample_questions": [
                "আমার কিস্তি জমা হয়েছে কিন্তু আপডেট হয়নি",
                "ticket status 1",
                "আমার শাখার নম্বর কী?",
                "ঋণের তথ্য জানতে চাই",
                "সঞ্চয়ের তথ্য জানতে চাই",
                "Show my loan summary for LOANEENO 0078114751",
                "কিস্তি জমা দেওয়ার নিয়ম কী?",
                "ঋণ নিতে কী কী কাগজপত্র লাগে?",
                "নতুন সদস্য হতে কী কী কাগজ লাগে?",
                "Show loanee list for branch code 0078",
            ]
        },
    )


def handle_customer_support_intent(user, intent, data):
    data["intent"] = intent

    if intent == "customer_verify":
        return handle_customer_verify(user, data)

    if intent == "installment_problem":
        return handle_installment_problem(user, data)

    if intent == "ticket_create":
        return handle_ticket_create(user, data)

    if intent == "ticket_status":
        return handle_ticket_status(user, data)

    if intent == "branch_info":
        return handle_branch_info(user, data)

    if intent == "loan_summary":
        return handle_loan_summary(user, data)

    if intent == "savings_summary":
        return handle_savings_summary(user, data)

    if intent == "escalate_ticket":
        return handle_escalate_ticket(user, data)

    return handle_general_support(user, data)


def handle_customer_support(request, question):
    try:
        # FAQ/process/policy question → Qdrant RAG
        if not is_live_customer_data_query(question):
            rag_result = answer_customer_support_rag(question)

            return JsonResponse(
                {
                    "ok": True,
                    "module": "customer_support",
                    "source": "qdrant_rag",
                    "intent": rag_result.get("intent", "general_faq"),
                    "answer": rag_result.get("answer", ""),
                    "rag_docs": rag_result.get("rag_docs", []),
                    "llm_used": rag_result.get("llm_used", False),
                    "llm_error": rag_result.get("llm_error", ""),
                },
                safe=True,
            )

        # Live data question → NiFi/Oracle
        result = call_customer_support_nifi(question)

        if hasattr(result, "status_code"):
            return result

        if not isinstance(result, dict):
            return JsonResponse(
                {
                    "ok": True,
                    "module": "customer_support",
                    "source": "nifi",
                    "intent": "live_customer_data",
                    "answer": str(result),
                },
                safe=True,
            )

        if not result.get("success"):
            return JsonResponse(
                {
                    "ok": False,
                    "module": "customer_support",
                    "source": "nifi",
                    "intent": "live_customer_data",
                    "called": result.get("called", True),
                    "enabled": result.get("enabled", True),
                    "success": False,
                    "status_code": result.get("status_code"),
                    "error": result.get("error") or "nifi_failed",
                    "message": result.get("message") or "NiFi request failed.",
                    "nifi_result": result,
                },
                safe=True,
            )

        data = result.get("data", [])

        if is_loanee_list_by_branch_query(question):
            answer = format_loanee_list_by_branch_answer(question, data)
            response_intent = "list_loanee_no_by_branch"
        else:
            answer = explain_loan_summary_with_llm(question, data)
            response_intent = "loan_summary"

        return JsonResponse(
            {
                "ok": True,
                "module": "customer_support",
                "source": "nifi_oracle",
                "intent": response_intent,
                "called": result.get("called", True),
                "enabled": result.get("enabled", True),
                "success": True,
                "status_code": result.get("status_code", 200),
                "BRANCHCODE": extract_branchcode_from_question(question),
                "LOANEENO": extract_loaneeno_from_question(question),
                "answer": answer,
                "data": data,
            },
            safe=True,
        )

    except Exception as e:
        return JsonResponse(
            {
                "ok": False,
                "module": "customer_support",
                "error": f"Customer support handler error: {e}",
            },
            status=500,
        )
