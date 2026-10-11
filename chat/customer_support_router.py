import re


CUSTOMER_SUPPORT_INTENTS = {
    "installment_problem": [
        "কিস্তি জমা হয়েছে কিন্তু আপডেট হয়নি",
        "কিস্তি আপডেট হয়নি",
        "জমা দিয়েছি কিন্তু আপডেট হয়নি",
        "জমা দিছি কিন্তু দেখাচ্ছে না",
        "payment not updated",
        "installment not updated",
        "installment problem",
        "kisti update hoy nai",
        "paid but not updated",
    ],
    "ticket_create": [
        "complaint",
        "অভিযোগ",
        "ticket create",
        "create ticket",
        "support ticket",
        "new ticket",
        "সমস্যা জানাতে চাই",
    ],
    "ticket_status": [
        "ticket status",
        "complaint status",
        "টিকিটের অবস্থা",
        "অভিযোগের অবস্থা",
        "ticket check",
    ],
    "branch_info": [
        "branch contact",
        "branch address",
        "branch information",
        "branch info",
        "শাখার নম্বর",
        "শাখার ঠিকানা",
        "শাখার তথ্য",
    ],
    "loan_summary": [
        "loan summary",
        "loan balance",
        "loan outstanding",
        "outstanding",
        "ঋণের তথ্য",
        "ঋণের অবস্থা",
    ],
    "savings_summary": [
        "savings balance",
        "saving balance",
        "savings summary",
        "সঞ্চয়ের তথ্য",
        "সঞ্চয়",
    ],
    "customer_verify": [
        "verify customer",
        "customer verification",
        "verify me",
        "member verify",
        "আমার তথ্য যাচাই",
    ],
    "escalate_ticket": [
        "escalate",
        "higher authority",
        "head office complaint",
        "area manager",
        "উর্ধ্বতন কর্তৃপক্ষ",
    ],
}


def normalize_text(text):
    if not text:
        return ""

    text = str(text).strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


def detect_customer_support_intent(message):
    msg = normalize_text(message)

    if not msg:
        return "general_support"

    for intent, patterns in CUSTOMER_SUPPORT_INTENTS.items():
        for pattern in patterns:
            if normalize_text(pattern) in msg:
                return intent

    if any(x in msg for x in ["কিস্তি", "installment", "payment", "paid", "জমা"]):
        return "installment_problem"

    if any(x in msg for x in ["ticket", "টিকিট", "অভিযোগ", "complaint"]):
        return "ticket_status"

    if any(x in msg for x in ["loan", "ঋণ", "outstanding"]):
        return "loan_summary"

    if any(x in msg for x in ["savings", "saving", "সঞ্চয়"]):
        return "savings_summary"

    if any(x in msg for x in ["branch", "শাখা"]):
        return "branch_info"

    return "general_support"


def extract_ticket_id(message):
    msg = normalize_text(message)
    match = re.search(r"\b(\d{1,10})\b", msg)
    if match:
        return match.group(1)
    return None
