from __future__ import annotations

import re
from typing import Callable, Dict, List, Optional, Tuple

from .normalize import normalize_text
from .entities import extract_entities
from .schema import IntentResult
from dataclasses import dataclass

@dataclass
class IntentRule:
    intent: str
    # returns True if matches question (normalized)
    match: Callable[[str], bool]
    # required entity names e.g. ["customerid"]
    required: List[str]
    base_confidence: float = 0.85


def _has(e, name: str) -> bool:
    return getattr(e, name, None) not in (None, "", 0)


def classify(
    module: str,
    question: str,
    rules: List["IntentRule"],
    default_limit: int,
    max_limit: int,
) -> IntentResult:
    raw, norm = normalize_text(question)
    entities = extract_entities(norm, default_limit, max_limit)

    best: Optional[Tuple[str, float]] = None
    best_meta: Dict[str, object] = {}

    for r in rules:
        if not r.match(norm):
            continue

        missing = [x for x in r.required if not _has(entities, x)]
        conf = r.base_confidence
        if missing:
            conf -= 0.35  # penalize missing key entities

        # small boost if explicit customerid token exists
        if "customerid" in norm.lower() and entities.customerid:
            conf += 0.05

        conf = max(0.0, min(0.99, conf))
        if best is None or conf > best[1]:
            best = (r.intent, conf)
            best_meta = {"missing_required": missing}

    if not best:
        return IntentResult(
            module=module,
            intent="unsupported",
            confidence=0.0,
            route="sqlcoder",
            entities=entities,
            raw_question=raw,
            normalized_question=norm,
            meta={"missing_required": []},
        )

    intent, conf = best
    route = "deterministic" if conf >= 0.70 else "sqlcoder"
    return IntentResult(
        module=module,
        intent=intent,
        confidence=conf,
        route=route,
        entities=entities,
        raw_question=raw,
        normalized_question=norm,
        meta=best_meta,
    )
