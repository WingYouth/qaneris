"""Compose an answer from an executed result without changing its facts."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from smartdata.common.redaction import SecretRedactor
from smartdata.contracts.query import GroundedQueryResult
from smartdata.security import is_sensitive_field

logger = logging.getLogger(__name__)

_NUMBER = re.compile(r"(?<![0-9A-Za-z])[-+]?\d+(?:,\d{3})*(?:\.\d+)?%?(?![0-9A-Za-z])")
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|private[_-]?key)"
    r"\s*[:=]\s*([^\s,;]+)"
)
_BEARER_TOKEN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_API_KEY_TOKEN = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
_PEM_MATERIAL = re.compile(r"-----BEGIN [^-\r\n]+-----.*?-----END [^-\r\n]+-----", re.DOTALL)
_PRIVATE_COLUMN = re.compile(r"(?i)(connection[_ -]?(string|url)|certificate|tls[_ -]?material)")


class AnswerModel(Protocol):
    def answer_question(self, question: str, result: dict[str, Any]) -> str: ...


@dataclass(frozen=True)
class AnswerComposition:
    text: str
    source: Literal["model", "deterministic_fallback"]


class AnswerComposer:
    """Give the model only a redacted result projection and reject unsupported numbers."""

    def __init__(self, model: AnswerModel | None):
        self.model = model

    def compose(
        self,
        *,
        question: str,
        result: GroundedQueryResult,
        analysis: dict[str, Any],
        deterministic_fallback: str,
    ) -> AnswerComposition:
        fallback = AnswerComposition(deterministic_fallback, "deterministic_fallback")
        if self.model is None:
            return fallback

        safe = result.model_dump(mode="json")
        columns = [name for name in result.columns if not _private_field(name)]
        rows = [{name: row.get(name) for name in columns} for row in safe["rows"]]
        numeric = analysis.get("numeric_summary", {})
        summary = (
            {str(name): values for name, values in numeric.items() if not _private_field(str(name))}
            if isinstance(numeric, dict)
            else {}
        )
        redactor = SecretRedactor.from_environment()
        safe_question = _safe_text(question, redactor)
        projection = {
            "columns": columns,
            "rows": rows,
            "row_count": result.row_count,
            "truncated": result.truncated,
            "deterministic_numeric_summary": summary,
        }
        # One final scrub also catches credential-shaped values in non-sensitive columns.
        projection = _safe_value(projection, redactor)
        guard = NumberEvidenceGuard(safe_question, projection)
        try:
            answer = self.model.answer_question(safe_question, projection).strip()
        except Exception as error:  # noqa: BLE001 - an executed query must survive model failures
            logger.warning(
                "answer model unavailable after successful query: %s", type(error).__name__
            )
            return fallback
        if not answer:
            return fallback
        answer = _safe_text(answer, redactor)
        if not guard.allows(answer):
            logger.warning("answer model introduced unsupported numeric tokens")
            return fallback
        if result.truncated and not any(term in answer for term in ("截断", "部分", "上限")):
            return fallback
        return AnswerComposition(answer, "model")


class NumberEvidenceGuard:
    """Share the same fail-closed numeric token rule across Ask and diagnosis."""

    def __init__(self, *evidence: Any):
        self.allowed = set().union(
            *(
                _number_tokens(json.dumps(item, ensure_ascii=False, default=str))
                for item in evidence
            )
        )

    def allows(self, answer: str) -> bool:
        return _number_tokens(answer).issubset(self.allowed)


def _number_tokens(value: str) -> set[str]:
    return {match.group().replace(",", "") for match in _NUMBER.finditer(value)}


def _private_field(name: str) -> bool:
    return is_sensitive_field(name) or bool(_PRIVATE_COLUMN.search(name))


def _safe_text(value: str, redactor: SecretRedactor) -> str:
    safe = redactor.text(value)
    safe = _PEM_MATERIAL.sub("<redacted>", safe)
    safe = _BEARER_TOKEN.sub("Bearer <redacted>", safe)
    safe = _API_KEY_TOKEN.sub("<redacted>", safe)
    return _CREDENTIAL_ASSIGNMENT.sub(r"\1=<redacted>", safe)


def _safe_value(value: Any, redactor: SecretRedactor) -> Any:
    if isinstance(value, str):
        return _safe_text(value, redactor)
    if isinstance(value, dict):
        return {
            str(name): "<redacted>" if _private_field(str(name)) else _safe_value(item, redactor)
            for name, item in value.items()
        }
    if isinstance(value, list):
        return [_safe_value(item, redactor) for item in value]
    return value
