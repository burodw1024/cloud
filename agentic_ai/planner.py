import re


def detect_language(query: str) -> str:
    if not query:
        return "en"

    bangla_chars = re.findall(r"[\u0980-\u09FF]", query)
    return "bn" if len(bangla_chars) >= 2 else "en"


def plan_query(query: str) -> dict:
    """
    Simple rule-based planner.
    Later you can replace this with LLM-based JSON planner.
    """
    q = (query or "").lower()
    language = detect_language(query)

    website_words = [
        "website", "burobd", "buro bd", "about buro", "mission", "vision",
        "program", "service", "loan product", "career", "publication",
        "বুরো", "ওয়েবসাইট", "সেবা", "প্রোগ্রাম"
    ]

    notice_words = [
        "notice", "hr notice", "circular", "noc", "latest notice",
        "নোটিশ", "সার্কুলার"
    ]

    policy_words = [
        "policy", "allowance", "transfer policy", "provident fund",
        "service rule", "grade structure", "recruitment policy",
        "নীতিমালা", "ভাতা", "বদলি", "প্রভিডেন্ট"
    ]

    support_words = [
        "customer support", "complaint", "installment problem",
        "loan status", "savings balance", "ticket",
        "অভিযোগ", "কিস্তি", "সাপোর্ট"
    ]

    data_words = [
        "top", "highest", "lowest", "outstanding", "loan summary",
        "branch", "cashflow", "overdue", "portfolio", "disbursement",
        "collection", "find top", "analytics", "report",
        "রিপোর্ট", "শাখা", "লোন", "বকেয়া"
    ]

    email_words = [
        "email", "mail", "outlook", "draft email", "reply email",
        "ইমেইল", "মেইল"
    ]

    teams_words = [
        "teams", "channel", "meeting summary", "teams message",
        "টিমস", "মিটিং"
    ]

    if any(w in q for w in email_words):
        return {
            "intent": "email_action",
            "tool": "outlook_tool",
            "language": language,
            "risk_level": "high",
            "requires_approval": True,
            "params": {"query": query},
        }

    if any(w in q for w in teams_words):
        return {
            "intent": "teams_search",
            "tool": "teams_tool",
            "language": language,
            "risk_level": "medium",
            "requires_approval": False,
            "params": {"query": query},
        }

    if any(w in q for w in notice_words):
        return {
            "intent": "notice_question",
            "tool": "notice_tool",
            "language": language,
            "risk_level": "low",
            "requires_approval": False,
            "params": {"query": query},
        }

    if any(w in q for w in policy_words):
        return {
            "intent": "policy_question",
            "tool": "policy_tool",
            "language": language,
            "risk_level": "low",
            "requires_approval": False,
            "params": {"query": query},
        }

    if any(w in q for w in support_words):
        return {
            "intent": "customer_support",
            "tool": "customer_support_tool",
            "language": language,
            "risk_level": "medium",
            "requires_approval": False,
            "params": {"query": query},
        }

    if any(w in q for w in data_words):
        return {
            "intent": "datawarehouse_query",
            "tool": "nifi_tool",
            "language": language,
            "risk_level": "medium",
            "requires_approval": False,
            "params": {"query": query},
        }

    if any(w in q for w in website_words):
        return {
            "intent": "website_rag",
            "tool": "website_rag_tool",
            "language": language,
            "risk_level": "low",
            "requires_approval": False,
            "params": {"query": query},
        }

    return {
        "intent": "general_or_website_rag",
        "tool": "website_rag_tool",
        "language": language,
        "risk_level": "low",
        "requires_approval": False,
        "params": {"query": query},
    }
