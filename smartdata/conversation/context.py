"""Bounded context and guarded business-language follow-up resolution."""

import json
import re
from typing import Any, Protocol

from smartdata.contracts.ask_events import _safe_value
from smartdata.conversation.models import Message, SemanticMemory
from smartdata.semantic.intent import RuleExtractor


class ConversationContextModel(Protocol):
    def resolve_followup(self, question: str, context: dict[str, Any]) -> str: ...


def working_set(
    messages: list[Message], memory: SemanticMemory, max_messages: int = 8, max_chars: int = 4000
) -> dict[str, Any]:
    semantic = memory.model_dump(mode="json")
    if len(json.dumps(semantic, ensure_ascii=False)) > max_chars // 2:
        semantic = {
            "active_metrics": [item[:100] for item in memory.active_metrics[:8]],
            "active_dimensions": [item[:100] for item in memory.active_dimensions[:8]],
            "active_time_expression": (memory.active_time_expression or "")[:100],
            "active_filters": [
                {key: str(value)[:100] for key, value in item.items()}
                for item in memory.active_filters[:8]
            ],
            "requested_output": [item[:100] for item in memory.requested_output[:8]],
            "last_run_id": memory.last_run_id,
            "diagnostic_findings": [
                {
                    "target_summary": str(item.get("target_summary", ""))[:120],
                    "statement": str(item.get("statement", ""))[:240],
                    "evidence_refs": item.get("evidence_refs", [])[-3:],
                    "scan_version": item.get("scan_version"),
                    "observed_at": item.get("observed_at"),
                }
                for item in memory.diagnostic_findings[-2:]
            ],
        }
    remaining = max(0, max_chars - len(json.dumps(semantic, ensure_ascii=False)))
    selected: list[dict[str, str]] = []
    for message in reversed(messages[-max_messages:]):
        if remaining <= 0:
            break
        content = message.content[:remaining]
        selected.append({"role": message.role, "content": content})
        remaining -= len(content)
    selected.reverse()
    return {
        "messages": selected,
        "confirmed_semantic_memory": semantic,
        "evidence_refs": memory.evidence_refs[-3:],
    }


_ELLIPTICAL = re.compile(r"^(那|再|只看|跟|与刚才|和刚才|刚才|它|这个|这些|按|换成|还有|继续)")
_PHYSICAL = re.compile(
    r"(?i)\b(SELECT|INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|FROM|WHERE|"
    r"db\.|\$match|\$group|HGETALL|SMEMBERS|LRANGE|MGET)\b"
    r"|\b[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*\b"
)


def _facts(question: str) -> tuple[set[str], set[str], set[str], str | None]:
    rules = RuleExtractor().extract(question)
    dates = {item.isoformat() for item in rules.explicit_dates}
    numbers = {str(value) for value in rules.numeric_values}
    filters = {f"{item.subject}:{item.operator}:{item.value}" for item in rules.filters}
    filters.update(
        f"只看:{match}" for match in re.findall(r"只看\s*([\u4e00-\u9fff]{2,8})", question)
    )
    return dates, numbers, filters, rules.requested_datasource_id


class ConversationContextResolver:
    def __init__(self, model: ConversationContextModel | None):
        self.model = model

    def resolve(self, question: str, memory: SemanticMemory, messages: list[Message]) -> str:
        if not memory.confirmed_business_query or not _ELLIPTICAL.search(question.strip()):
            return question
        if self.model is None:
            raise RuntimeError("conversation_context_model_unavailable")
        context = working_set(messages, memory)
        resolved = self.model.resolve_followup(question, context).strip()
        if (
            not resolved
            or len(resolved) > 2000
            or _PHYSICAL.search(resolved)
            or _safe_value(resolved) != resolved
        ):
            raise ValueError("invalid_context_resolution")
        original = _facts(question)
        result = _facts(resolved)
        if any(not a.issubset(b) for a, b in zip(original[:3], result[:3], strict=True)):
            raise ValueError("context_rule_preservation_failed")
        if result[3] != original[3]:
            raise ValueError("context_rule_preservation_failed")
        return resolved
