"""Fail-closed diagnostic question policy."""

import re

from smartdata.diagnostics.models import DiagnosticBudget, DiagnosticDecision, EvidenceQuestion

_NATIVE = re.compile(
    r"(?i)\b(select|insert|update|delete|drop|alter|create|from|where|join|"
    r"hgetall|smembers|lrange|mget|db\.|datasource_id|table|field)\b"
    r"|\$match|\$group|\b[A-Za-z_]\w*\.[A-Za-z_]\w*\b"
)


def question_key(question: str) -> str:
    return " ".join(question.strip().casefold().split())


def validate_question(question: EvidenceQuestion) -> None:
    if _NATIVE.search(question.question) or _NATIVE.search(question.purpose):
        raise ValueError("diagnostic question contains native query material")
    if not question.question.strip() or not question.purpose.strip():
        raise ValueError("empty diagnostic question")


def select_questions(
    decision: DiagnosticDecision,
    seen: set[str],
    remaining: int,
    budget: DiagnosticBudget,
) -> tuple[list[EvidenceQuestion], list[EvidenceQuestion]]:
    selected: list[EvidenceQuestion] = []
    duplicates: list[EvidenceQuestion] = []
    if decision.action != "query":
        return selected, duplicates
    for question in sorted(decision.questions, key=lambda item: item.priority):
        validate_question(question)
        key = question_key(question.question)
        if key in seen:
            duplicates.append(question)
        elif len(selected) < min(remaining, budget.max_questions_per_round):
            selected.append(question)
            seen.add(key)
    return selected, duplicates
