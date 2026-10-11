from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class DateRange:
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    preset: Optional[str] = None  # today, this_month, last_7_days, last_30_days, ...


@dataclass
class Entities:
    customerid: Optional[str] = None
    empid: Optional[str] = None
    branch: Optional[str] = None
    district: Optional[str] = None
    status: Optional[str] = None  # active/inactive
    date: DateRange = field(default_factory=DateRange)
    limit: int = 200


@dataclass
class IntentResult:
    module: str
    intent: str
    confidence: float
    route: str  # deterministic | sqlcoder | llm_intent (optional)
    entities: Entities
    raw_question: str
    normalized_question: str
    meta: Dict[str, Any] = field(default_factory=dict)
