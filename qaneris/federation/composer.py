"""Answer from merged facts with the shared numeric evidence guard."""

from qaneris.answering.composer import NumberEvidenceGuard, _safe_text, _safe_value
from qaneris.common.redaction import SecretRedactor
from qaneris.federation.models import FederatedEvidence, MergedResult


class FederatedAnswerComposer:
    def __init__(self, model=None):
        self.model = model

    def compose(self, question: str, result: MergedResult, evidence: FederatedEvidence):
        redactor = SecretRedactor.from_environment()
        safe_question = _safe_text(question, redactor)
        safe_rows = _safe_value(result.rows, redactor)
        source_lines = [
            f"{item['datasource_id']}：{item['source_started_at']} 至 {item['source_completed_at']}"
            for item in evidence.source_evidence
        ]
        fallback = (
            f"联合分析完成（{result.operation}）。结果：{safe_rows}。"
            f"各源返回行数：{evidence.input_row_counts}。"
            f"各源独立取数时间：{'；'.join(source_lines)}。"
            "不同数据源不保证来自同一事务快照。"
        )
        if result.truncated:
            fallback += "结果受行数上限截断。"
        if self.model is None:
            return fallback, "federated_deterministic_fallback"
        projection = {
            "columns": result.columns, "rows": safe_rows,
            "truncated": result.truncated, "merge_operation": result.operation,
            "source_timestamps": source_lines,
        }
        try:
            answer = self.model.answer_question(safe_question, projection).strip()
        except Exception:  # noqa: BLE001 - successful reads and merge remain successful
            return fallback, "federated_deterministic_fallback"
        answer = _safe_text(answer, redactor)
        if not answer or not NumberEvidenceGuard(safe_question, safe_rows).allows(answer):
            return fallback, "federated_deterministic_fallback"
        if "事务快照" not in answer:
            answer += " 不同数据源不保证来自同一事务快照。"
        if result.truncated and not any(word in answer for word in ("截断", "部分", "上限")):
            return fallback, "federated_deterministic_fallback"
        return answer, "model"
