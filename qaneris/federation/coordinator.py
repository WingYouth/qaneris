"""Durable source tasks, each executed through the existing single-source Ask port."""

import json
from datetime import UTC, datetime, timedelta

from qaneris.answering.composer import _safe_value
from qaneris.common.redaction import SecretRedactor
from qaneris.contracts import AskRequest, AskResponse, AskStatus
from qaneris.contracts.semantic import BusinessQuery
from qaneris.conversation.models import SemanticMemory, now
from qaneris.federation.composer import FederatedAnswerComposer
from qaneris.federation.governance import FederationFailure, JoinMappingGovernance
from qaneris.federation.merger import ControlledMerger
from qaneris.federation.models import (
    ExecutionScope,
    FederatedEvidence,
    FederatedPlanDraft,
    FederationBudget,
    SourceTaskStatus,
)
from qaneris.federation.ports import FederatedPlanningContext
from qaneris.federation.repository import SQLiteFederationRepository
from qaneris.federation.validator import FederatedPlanValidator
from qaneris.runtime.models import RunStatus
from qaneris.runtime.state_machine import transition

_TRANSIENT = {
    "model_invocation_failed", "graph_unavailable", "datasource_unavailable",
    "timeout", "temporary_unavailable", "rate_limited", "transient_network",
    "task_interrupted", "run_interrupted",
}
_OPERATIONS = [
    "compare_scalars", "combine_scalars", "union_rows", "align_time_series", "keyed_join"
]


class FederatedQuestionCapability:
    def __init__(self, runtime, repository: SQLiteFederationRepository, service=None,
                 planning_model=None, budget: FederationBudget | None = None):
        self.runtime = runtime
        self.repository = repository
        self.service = service
        self.model = planning_model
        self.budget = budget or FederationBudget()
        self.governance = JoinMappingGovernance(repository, service) if service else None
        self.validator = FederatedPlanValidator(self.governance) if self.governance else None
        self.merger = ControlledMerger()
        self.composer = FederatedAnswerComposer(planning_model)
        self.repository.recover_running()

    def _event(self, run_id: str, kind: str, **payload):
        self.runtime.runs.append_event(run_id, kind, payload)

    def _scope(self, workspace_id: str, allowed: list[str], max_rows: int) -> ExecutionScope:
        budget = self.budget
        return ExecutionScope(
            workspace_id=workspace_id,
            allowed_datasource_ids=allowed,
            max_sources=min(budget.max_sources, len(allowed)),
            max_rows_per_source=min(max_rows, budget.max_rows_per_source),
            max_total_rows=budget.max_total_rows,
            max_intermediate_bytes=budget.max_intermediate_bytes,
            deadline=now() + timedelta(seconds=budget.deadline_seconds),
        )

    def drive(self, run, question: str, memory, allowed: list[str], decision,
              clarifications: list[str]):
        existing = self.repository.plan(run.run_id)
        if existing is None:
            if self.model is None or self.validator is None:
                raise FederationFailure("federation_planner_unavailable", blocked=True)
            scope = self._scope(run.workspace_id, allowed, run.max_rows)
            mapping_summaries = [
                {
                    "mapping_id": item.mapping_id,
                    "business_key": item.business_key,
                    "left_datasource_id": item.left_datasource_id,
                    "right_datasource_id": item.right_datasource_id,
                    "left_grain": item.left_grain,
                    "right_grain": item.right_grain,
                    "cardinality": item.cardinality,
                }
                for item in self.repository.mappings(run.workspace_id)
                if item.status == "CONFIRMED"
                and {item.left_datasource_id, item.right_datasource_id} <= set(allowed)
            ]
            context = FederatedPlanningContext(
                question=question,
                business_query=decision.business_query,
                sources=decision.sources,
                confirmed_join_mappings=mapping_summaries,
                confirmed_semantic_memory={
                    "active_metrics": memory.active_metrics,
                    "active_dimensions": memory.active_dimensions,
                    "active_time_expression": memory.active_time_expression,
                },
                allowed_merge_operations=_OPERATIONS,
                execution_scope=scope,
            )
            self._event(run.run_id, "FEDERATION_DISCOVERY_STARTED",
                        allowed_datasource_ids=allowed)
            try:
                draft = FederatedPlanDraft.model_validate(self.model.plan_federation(context))
            except Exception as error:
                if isinstance(error, FederationFailure):
                    raise
                raise FederationFailure("federation_planner_unavailable", blocked=True) from error
            plan = self.validator.validate(
                draft, scope, run.run_id, question, decision.business_query
            )
            plan = self.repository.create_plan(plan)
            self._event(run.run_id, "FEDERATED_PLAN_READY",
                        plan_id=plan.plan_id, operation=plan.merge_plan.operation,
                        datasource_ids=[task.datasource_id for task in plan.source_tasks])
        else:
            plan = existing
            if {task.datasource_id for task in plan.source_tasks} - set(allowed):
                raise FederationFailure("forbidden_datasource", blocked=True)
            if plan.merge_plan.mapping_id:
                self.governance.executable(plan.merge_plan.mapping_id, run.workspace_id,
                                           [task.datasource_id for task in plan.source_tasks])
        run = self.runtime._advance(run, RunStatus.PLANNING)
        run = self.runtime._advance(run, RunStatus.EXECUTING)
        for task in self.repository.tasks(run.run_id):
            if self.runtime._cancel_if_requested(run):
                return
            if datetime.now(UTC) >= plan.execution_scope.deadline:
                raise FederationFailure("federation_deadline_exceeded")
            if task.status == SourceTaskStatus.COMPLETED:
                continue
            if task.status == SourceTaskStatus.WAITING_USER and not clarifications:
                return
            if task.status == SourceTaskStatus.FAILED and not task.retryable:
                raise FederationFailure(task.failure_code or "source_task_failed")
            self._execute_task(run, task, plan.execution_scope, clarifications[-1]
                               if task.status == SourceTaskStatus.WAITING_USER else None)
            if self.runtime.runs.get(run.run_id).status == RunStatus.WAITING_USER:
                return
        if self.runtime._cancel_if_requested(run):
            return
        tasks = self.repository.tasks(run.run_id)
        if any(task.status != SourceTaskStatus.COMPLETED for task in tasks):
            raise FederationFailure("source_tasks_incomplete")
        responses = {task.task_id: AskResponse.model_validate(task.response_json) for task in tasks}
        mapping = (self.governance.executable(
            plan.merge_plan.mapping_id, run.workspace_id,
            [task.datasource_id for task in tasks],
        ) if plan.merge_plan.mapping_id else None)
        self._event(run.run_id, "MERGE_STARTED", operation=plan.merge_plan.operation)
        merged = self.merger.merge(plan.merge_plan, responses, plan.execution_scope, mapping)
        evidence = FederatedEvidence(
            source_evidence=[{
                "task_id": task.task_id,
                "datasource_id": task.datasource_id,
                "scan_version": responses[task.task_id].evidence.scan_version,
                "source_started_at": task.started_at.isoformat(),
                "source_completed_at": task.completed_at.isoformat(),
                "row_count": responses[task.task_id].result.row_count,
                "truncated": responses[task.task_id].result.truncated,
            } for task in tasks],
            merge_operation=plan.merge_plan.operation,
            merge_mapping_id=plan.merge_plan.mapping_id,
            input_row_counts={task.task_id: responses[task.task_id].result.row_count
                              for task in tasks},
            output_row_count=merged.row_count,
            truncated=merged.truncated,
            warnings=merged.warnings,
        )
        self._event(run.run_id, "MERGE_COMPLETED", row_count=merged.row_count)
        run = self.runtime._advance(run, RunStatus.ANSWERING)
        answer, answer_source = self.composer.compose(question, merged, evidence)
        response_json = {
            "question": question, "answer": answer, "answer_source": answer_source,
            "federated_plan": {
                "plan_id": plan.plan_id,
                "operation": plan.merge_plan.operation,
                "datasource_ids": [task.datasource_id for task in tasks],
            },
            "source_tasks": [{
                "task_id": task.task_id, "datasource_id": task.datasource_id,
                "status": task.status, "attempt": task.attempt,
            } for task in tasks],
            "merged_result": _safe_value(
                merged.model_dump(mode="json"), SecretRedactor.from_environment()
            ),
            "federated_evidence": evidence.model_dump(mode="json"),
        }
        run = self.runtime._save(transition(
            run, RunStatus.COMPLETED, response_json=response_json, completed_at=now()
        ))
        self.runtime.messages.assistant_message(run.conversation_id, answer, run.run_id)
        self._record_memory(run, plan, merged, memory)
        self._event(run.run_id, "RUN_COMPLETED")

    def _execute_task(self, run, task, scope, clarification: str | None):
        started = now()
        question = (f"{task.question}。用户澄清：{clarification}" if clarification
                    else task.question)
        task = self.repository.save_task(task.model_copy(update={
            "status": SourceTaskStatus.RUNNING, "attempt": task.attempt + 1,
            "started_at": started, "failure_code": None, "retryable": False,
        }))
        self._event(run.run_id, "SOURCE_TASK_STARTED", source_task_id=task.task_id,
                    datasource_id=task.datasource_id, attempt=task.attempt)
        response = None
        failure = None
        records = iter(self.runtime.ask.execute(AskRequest(
            question=question, workspace_id=scope.workspace_id,
            datasource_id=task.datasource_id, max_rows=scope.max_rows_per_source,
        )))
        while True:
            if self.runtime._cancel_if_requested(run):
                close = getattr(records, "close", None)
                if close:
                    close()
                self.repository.save_task(task.model_copy(update={
                    "status": SourceTaskStatus.SKIPPED,
                    "failure_code": "cancelled",
                }))
                return
            try:
                record = next(records)
            except StopIteration:
                break
            self._event(run.run_id, "ASK_PROGRESS", source_task_id=task.task_id,
                        datasource_id=task.datasource_id,
                        ask_event_type=record.event.event_type.value)
            if record.response is not None:
                response = record.response
            if record.exception is not None:
                failure = record.exception
        if self.runtime._cancel_if_requested(run):
            self.repository.save_task(task.model_copy(update={
                "status": SourceTaskStatus.SKIPPED,
                "failure_code": "cancelled",
            }))
            return
        if failure or response is None:
            code = getattr(failure, "code", "ask_failed")
            self._fail_task(run, task, code)
        if response.status == AskStatus.CLARIFICATION_REQUIRED:
            message = response.clarification[0].question if response.clarification else response.answer
            self.repository.save_task(task.model_copy(update={
                "status": SourceTaskStatus.WAITING_USER,
                "response_json": response.model_dump(mode="json"),
            }))
            current = self.runtime.runs.get(run.run_id)
            current = self.runtime._save(transition(
                current, RunStatus.WAITING_USER,
                response_json={"clarification": message, "source_task_id": task.task_id},
            ))
            self.runtime.messages.assistant_message(
                run.conversation_id, message, run.run_id, kind="clarification",
                checkpoint_revision=current.revision,
            )
            self._event(run.run_id, "CLARIFICATION_REQUIRED", source_task_id=task.task_id)
            return
        if response.status != AskStatus.COMPLETED or response.result is None \
                or response.evidence is None or response.evidence.datasource_id != task.datasource_id:
            code = response.error.code if response.error else "source_unverified_result"
            self._fail_task(run, task, code)
        if response.result.row_count > scope.max_rows_per_source \
                or len(json.dumps(response.result.rows, ensure_ascii=False, default=str).encode()) \
                > scope.max_intermediate_bytes:
            self._fail_task(run, task, "result_limit")
        completed_rows = 0
        completed_bytes = 0
        for previous in self.repository.tasks(run.run_id):
            if previous.status != SourceTaskStatus.COMPLETED:
                continue
            result = AskResponse.model_validate(previous.response_json).result
            completed_rows += len(result.rows)
            completed_bytes += len(json.dumps(result.rows, ensure_ascii=False, default=str).encode())
        if completed_rows + len(response.result.rows) > scope.max_total_rows or (
            completed_bytes + len(json.dumps(response.result.rows, ensure_ascii=False,
                                             default=str).encode()) > scope.max_intermediate_bytes
        ):
            self._fail_task(run, task, "result_limit")
        completed = self.repository.save_task(task.model_copy(update={
            "status": SourceTaskStatus.COMPLETED,
            "response_json": response.model_dump(mode="json"),
            "completed_at": now(),
        }))
        self._event(run.run_id, "SOURCE_TASK_COMPLETED", source_task_id=task.task_id,
                    datasource_id=task.datasource_id, row_count=response.result.row_count,
                    source_started_at=completed.started_at.isoformat(),
                    source_completed_at=completed.completed_at.isoformat())

    def _fail_task(self, run, task, code: str):
        retryable = code in _TRANSIENT
        self.repository.save_task(task.model_copy(update={
            "status": SourceTaskStatus.FAILED, "failure_code": code,
            "retryable": retryable,
        }))
        self._event(run.run_id, "SOURCE_TASK_FAILED", source_task_id=task.task_id,
                    datasource_id=task.datasource_id, failure_code=code)
        raise FederationFailure(code, retryable=retryable)

    def _record_memory(self, run, plan, merged, previous: SemanticMemory):
        query = BusinessQuery.model_validate(plan.business_query)
        updated = SemanticMemory(
            confirmed_business_query=plan.business_query,
            selected_datasource_ids=[task.datasource_id for task in plan.source_tasks],
            active_metrics=query.metrics,
            active_dimensions=query.dimensions,
            active_time_expression=query.time_expression,
            active_filters=[item.model_dump(mode="json") for item in query.filters],
            requested_output=[item.value for item in query.requested_output],
            last_run_id=run.run_id,
            last_result_shape={"columns": merged.columns, "row_count": merged.row_count,
                               "truncated": merged.truncated},
            evidence_refs=[f"federated_evidence:{run.run_id}"],
            diagnostic_findings=previous.diagnostic_findings,
        )
        self.runtime.conversations.put_memory(
            run.conversation_id, updated,
            self.runtime.conversations.memory_revision(run.conversation_id),
        )
