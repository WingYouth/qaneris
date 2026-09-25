"""Replace active semantics only after a successful Ask result."""

from qaneris.contracts import AskResponse
from qaneris.conversation.models import SemanticMemory, now


def confirmed_memory(
    response: AskResponse, run_id: str, previous: SemanticMemory | None = None
) -> SemanticMemory:
    query = response.business_query
    if query is None:
        raise ValueError("completed Ask has no confirmed business query")
    result = response.result
    shape = None
    if result is not None:
        shape = {
            "columns": result.columns,
            "row_count": result.row_count,
            "truncated": result.truncated,
        }
    datasource_ids = [response.evidence.datasource_id] if response.evidence else []
    return SemanticMemory(
        confirmed_business_query=query.model_dump(mode="json"),
        selected_datasource_ids=datasource_ids,
        active_metrics=query.metrics,
        active_dimensions=query.dimensions,
        active_time_expression=query.time_expression,
        active_filters=[item.model_dump(mode="json") for item in query.filters],
        requested_output=[str(item.value) for item in query.requested_output],
        last_run_id=run_id,
        last_result_shape=shape,
        evidence_refs=[f"evidence:{run_id}"] if response.evidence else [],
        diagnostic_findings=previous.diagnostic_findings if previous else [],
        updated_at=now(),
    )
