"""Safe answer composition from verified diagnostic observations."""

import logging
import re

from smartdata.answering.composer import NumberEvidenceGuard, _safe_text
from smartdata.common.redaction import SecretRedactor
from smartdata.diagnostics.models import DiagnosticSynthesisContext
from smartdata.diagnostics.ports import DiagnosticModel

logger = logging.getLogger(__name__)
_CAUSAL = re.compile(r"(?:导致|造成|引起|根本原因是|证明.*?因果|caused? by)", re.IGNORECASE)


def deterministic_answer(context: DiagnosticSynthesisContext) -> str:
    lines = [f"已完成 {len(context.observations)} 个证据查询。", "数据显示："]
    for item in context.observations:
        lines.append(f"- {item.purpose}：{item.safe_answer or item.bounded_rows[:3]}")
    for item in context.unavailable:
        lines.append(f"- 未验证：{item.purpose}（{item.reason or '数据不可用'}）。")
    if any(item.result_shape.get("truncated") for item in context.observations):
        lines.append("部分结果已截断，分析仅基于返回的部分数据。")
    lines.append("这些观察反映数据库中的变化或相关性，当前证据不足以证明因果关系。")
    return "\n".join(lines)


class DiagnosticComposer:
    def __init__(self, model: DiagnosticModel | None):
        self.model = model

    def compose(
        self, context: DiagnosticSynthesisContext, original_question: str, semantic_memory: dict
    ) -> tuple[str, str]:
        fallback = deterministic_answer(context)
        if self.model is None:
            return fallback, "diagnostic_deterministic_fallback"
        redactor = SecretRedactor.from_environment()
        guard = NumberEvidenceGuard(
            original_question,
            {
                key: semantic_memory.get(key)
                for key in (
                    "active_metrics",
                    "active_dimensions",
                    "active_filters",
                    "active_time_expression",
                )
            },
            len(context.observations),
            [
                {"answer": item.safe_answer, "rows": item.bounded_rows}
                for item in context.observations
            ],
        )
        try:
            answer = _safe_text(self.model.synthesize_diagnosis(context).strip(), redactor)
        except Exception as error:  # noqa: BLE001 - verified evidence survives model outage
            logger.warning("diagnostic synthesis unavailable: %s", type(error).__name__)
            return fallback, "diagnostic_deterministic_fallback"
        if not answer or not guard.allows(answer) or _CAUSAL.search(answer):
            return fallback, "diagnostic_deterministic_fallback"
        if any(item.result_shape.get("truncated") for item in context.observations) and not any(
            word in answer for word in ("截断", "部分", "上限")
        ):
            return fallback, "diagnostic_deterministic_fallback"
        if context.unavailable and not any(
            word in answer for word in ("未验证", "没有", "缺少", "不可用")
        ):
            return fallback, "diagnostic_deterministic_fallback"
        return answer, "diagnostic_model"
