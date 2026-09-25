from __future__ import annotations

from smartdata.contracts.semantic import BusinessQuery
from smartdata.semantic.models import ClarificationRequest


class ClarificationBuilder:
    """Decide whether the *intent layer alone* must stop and ask the user.

    The intent model is explicitly asked to report any ambiguity it notices, and it does: on
    ordinary questions it returns notes such as "未指定时间范围" or "统计口径未明确（是否扣除退款）".
    Those notes are treated as **advisory evidence**, not as a verdict. A governed asset already
    states what a business term means, so a model's second-guessing of a definition cannot
    override governance, and letting free-text notes stop execution would make the pipeline's
    behaviour depend on the model's phrasing rather than on the published data graph.

    Only ``rule_ambiguities`` stop here: the rule layer could not turn part of the question into
    a business filter (for example the user typed a physical field path). Answering anyway would
    silently drop that constraint and return a confidently wrong number. Model confidence remains
    evidence; the published graph and grounding decide whether the query is executable.

    Everything else that can genuinely make a question unanswerable is decided by the physical
    layers — grounding refuses two governed definitions that resolve to different physical
    targets, the grounded context builder refuses an unresolved ground, and plan validation
    refuses a drifted plan. Those clarifications carry readable candidate labels.
    """

    def build(
        self, query: BusinessQuery, *, rule_ambiguities: list[str] | None = None
    ) -> list[ClarificationRequest]:
        requests = [
            ClarificationRequest(
                clarification_id=f"ambiguity_{index}",
                field="ambiguities",
                question=f"请确认：{ambiguity}",
            )
            for index, ambiguity in enumerate(rule_ambiguities or [], start=1)
        ]
        return requests
