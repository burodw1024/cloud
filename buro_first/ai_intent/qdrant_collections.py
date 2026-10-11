# buro_first/ai_intent/qdrant_collections.py

MODULE_SCHEMA_COLLECTION = {
    "cashflow": "dw_cashflow_auto",
    "hr": "dw_hr_auto",
    "policy": "buro_policy_auto",
    "public": "dw_public_auto",
}

def get_schema_collection(module: str) -> str:
    m = (module or "").strip().lower()
    return MODULE_SCHEMA_COLLECTION.get(m, "dw_cashflow_auto")  # safe default
