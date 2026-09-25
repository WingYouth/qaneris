"""Two-round evidence loop. Every task executes one complete Ask attempt."""

import json
import logging
from uuid import uuid4

from smartdata.answering.composer import _private_field, _safe_text, _safe_value
from smartdata.common.redaction import SecretRedactor
from smartdata.contracts import AskRequest, AskResponse, AskStatus
from smartdata.conversation.models import now
from smartdata.diagnostics.composer import DiagnosticComposer, deterministic_answer
from smartdata.diagnostics.models import (
    DiagnosticBudget,
    DiagnosticFinding,
    DiagnosticObservation,
    DiagnosticPlanningContext,
    DiagnosticSynthesisContext,
    DiagnosticTask,
    TaskStatus,
)
from smartdata.diagnostics.policy import question_key, select_questions
from smartdata.runtime.models import RunStatus
from smartdata.runtime.state_machine import transition
from smartdata.semantic.intent import RuleExtractor

logger = logging.getLogger(__name__)
_TRANSIENT = {
    "model_invocation_failed",
    "graph_unavailable",
    "datasource_unavailable",
    "timeout",
    "temporary_unavailable",
    "rate_limited",
    "transient_network",
    "execution_failed",
    "run_interrupted",
    "task_interrupted",
}
_MISSING = (
    "当前数据没有",
    "没有受治理",
    "没有找到",
    "缺少",
    "不存在",
    "未找到",
    "无法找到",
    "请先完成该业务定义的发布",
    "请先发布相应时间轴定义",
)


class DiagnosticFailure(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False, blocked: bool = False):
        self.code = code
        self.retryable = retryable
        self.blocked = blocked
        super().__init__(code)


class DiagnosticCoordinator:
    def __init__(self, runtime, repository, model, budget: DiagnosticBudget | None = None):
        self.runtime = runtime
        self.repository = repository
        self.model = model
        self.budget = budget or DiagnosticBudget()
        self.composer = DiagnosticComposer(model)
        self.repository.recover_running()

    def _event(self, run_id: str, kind: str, **payload):
        self.runtime.runs.append_event(run_id, kind, payload)

    def _cancelled(self, run) -> bool:
        return self.runtime._cancel_if_requested(run) is not None

    def _wait_for_clarification(self, run, question: str, message: str) -> None:
        from smartdata.contracts import AskClarification

        response = AskResponse(
            question=question,
            status=AskStatus.CLARIFICATION_REQUIRED,
            answer=message,
            clarification=[AskClarification(question=message)],
        )
        current = self.runtime.runs.get(run.run_id)
        current = self.runtime._save(
            transition(
                current,
                RunStatus.WAITING_USER,
                response_json=response.model_dump(mode="json"),
            )
        )
        self.runtime.messages.assistant_message(
            run.conversation_id,
            message,
            run.run_id,
            kind="clarification",
            checkpoint_revision=current.revision,
        )
        self._event(run.run_id, "CLARIFICATION_REQUIRED")

    def _observations(self, run_id: str) -> list[DiagnosticObservation]:
        observations = []
        for task in self.repository.tasks(run_id):
            if task.status != TaskStatus.COMPLETED or not task.ask_response_json:
                continue
            response = AskResponse.model_validate(task.ask_response_json)
            redactor = SecretRedactor.from_environment()
            if (
                response.status != AskStatus.COMPLETED
                or not response.evidence
                or not response.result
            ):
                observations.append(
                    DiagnosticObservation(
                        observation_id=f"observation:{task.id}",
                        task_id=task.id,
                        question=task.question,
                        purpose=task.purpose,
                        status="unavailable",
                        reason=_safe_text(response.answer, redactor)[:240],
                    )
                )
                continue
            result = response.result
            columns = [name for name in result.columns if not _private_field(name)]
            rows = [
                _safe_value({name: row.get(name) for name in columns}, redactor)
                for row in result.rows[: self.budget.max_observation_rows]
            ]
            observations.append(
                DiagnosticObservation(
                    observation_id=f"observation:{task.id}",
                    task_id=task.id,
                    question=task.question,
                    purpose=task.purpose,
                    status="verified",
                    safe_answer=(
                        f"返回 {result.row_count} 行；可见结果样例："
                        f"{json.dumps(rows[:3], ensure_ascii=False, default=str)}"
                    ),
                    result_shape={
                        "columns": columns,
                        "row_count": result.row_count,
                        "truncated": result.truncated or len(result.rows) > len(rows),
                    },
                    bounded_rows=rows,
                    evidence_ref=task.evidence_ref,
                    datasource_id=response.evidence.datasource_id,
                    scan_version=response.evidence.scan_version,
                    observed_at=task.completed_at or now(),
                )
            )
        return observations

    def _execute_task(self, run, task, conversation, checkpoint, clarification: str | None):
        run = self.runtime.runs.get(run.run_id)
        if self._cancelled(run):
            return checkpoint
        task = self.repository.save_task(
            task.model_copy(
                update={
                    "status": TaskStatus.RUNNING,
                    "attempt": task.attempt + 1,
                    "failure_code": None,
                    "failure_message": None,
                    "retryable": False,
                }
            )
        )
        checkpoint = self.repository.save_checkpoint(
            checkpoint.model_copy(
                update={
                    "pending_task_id": task.id,
                }
            )
        )
        self._event(
            run.run_id,
            "EVIDENCE_QUERY_STARTED",
            task_id=task.id,
            question=task.question,
            purpose=task.purpose,
            round=task.round,
        )
        question = f"{task.question}。用户澄清：{clarification}" if clarification else task.question
        request = AskRequest(
            question=question,
            workspace_id=conversation.workspace_id,
            datasource_id=checkpoint.diagnostic_datasource_id,
            max_rows=run.max_rows,
        )
        response = None
        failure = None
        records = iter(self.runtime.ask.execute(request))
        while True:
            if self._cancelled(run):
                close = getattr(records, "close", None)
                if close:
                    close()
                self.repository.save_task(task.model_copy(update={"status": TaskStatus.SKIPPED}))
                return checkpoint
            try:
                record = next(records)
            except StopIteration:
                break
            except Exception as error:
                code = getattr(error, "code", "ask_failed")
                retryable = code in _TRANSIENT
                self.repository.save_task(
                    task.model_copy(
                        update={
                            "status": TaskStatus.FAILED,
                            "failure_code": code,
                            "failure_message": code,
                            "retryable": retryable,
                        }
                    )
                )
                self._event(
                    run.run_id,
                    "EVIDENCE_QUERY_FAILED",
                    task_id=task.id,
                    failure_code=code,
                    retryable=retryable,
                )
                raise DiagnosticFailure(code, retryable=retryable) from error
            if record.event.event_type == "plan_ready":
                run = self.runtime._advance(run, RunStatus.PLANNING)
            elif record.event.event_type == "execution_started":
                run = self.runtime._advance(run, RunStatus.EXECUTING)
            elif record.event.event_type == "result_ready":
                run = self.runtime._advance(run, RunStatus.ANSWERING)
            self._event(
                run.run_id,
                "ASK_PROGRESS",
                task_id=task.id,
                ask_event_type=record.event.event_type.value,
                ask_sequence=record.event.sequence,
                ask_payload=record.event.payload,
            )
            if record.response is not None:
                response = record.response
            if record.exception is not None:
                failure = record.exception
        if self._cancelled(run):
            self.repository.save_task(task.model_copy(update={"status": TaskStatus.SKIPPED}))
            return checkpoint
        if response is None:
            self.repository.save_task(
                task.model_copy(
                    update={
                        "status": TaskStatus.FAILED,
                        "failure_code": "ask_failed",
                        "retryable": False,
                    }
                )
            )
            raise DiagnosticFailure("ask_failed")
        data = response.model_dump(mode="json")
        if response.status == AskStatus.CLARIFICATION_REQUIRED:
            message = (
                response.clarification[0].question if response.clarification else response.answer
            )
            if any(marker in message for marker in _MISSING) and not any(
                item.options for item in response.clarification
            ):
                task = self.repository.save_task(
                    task.model_copy(
                        update={
                            "status": TaskStatus.COMPLETED,
                            "ask_response_json": data,
                            "completed_at": now(),
                        }
                    )
                )
                checkpoint = self.repository.save_checkpoint(
                    checkpoint.model_copy(
                        update={
                            "pending_task_id": None,
                            "questions_completed": checkpoint.questions_completed + 1,
                        }
                    )
                )
                self._event(
                    run.run_id, "EVIDENCE_QUERY_UNAVAILABLE", task_id=task.id, reason="missing_data"
                )
                return checkpoint
            task = self.repository.save_task(
                task.model_copy(
                    update={
                        "status": TaskStatus.WAITING_USER,
                        "ask_response_json": data,
                    }
                )
            )
            current = self.runtime.runs.get(run.run_id)
            current = self.runtime._save(
                transition(
                    current,
                    RunStatus.WAITING_USER,
                    response_json=data,
                )
            )
            self.runtime.messages.assistant_message(
                run.conversation_id,
                message,
                run.run_id,
                kind="clarification",
                checkpoint_revision=current.revision,
            )
            self._event(run.run_id, "CLARIFICATION_REQUIRED", task_id=task.id)
            return checkpoint
        if response.status != AskStatus.COMPLETED or failure is not None:
            code = response.error.code if response.error else getattr(failure, "code", "ask_failed")
            retryable = code in _TRANSIENT
            self.repository.save_task(
                task.model_copy(
                    update={
                        "status": TaskStatus.FAILED,
                        "ask_response_json": data,
                        "failure_code": code,
                        "failure_message": response.answer[:240],
                        "retryable": retryable,
                    }
                )
            )
            self._event(
                run.run_id,
                "EVIDENCE_QUERY_FAILED",
                task_id=task.id,
                failure_code=code,
                retryable=retryable,
            )
            raise DiagnosticFailure(code, retryable=retryable)
        if response.evidence is None or response.result is None:
            self.repository.save_task(
                task.model_copy(
                    update={
                        "status": TaskStatus.FAILED,
                        "ask_response_json": data,
                        "failure_code": "diagnostic_unverified_result",
                        "retryable": False,
                    }
                )
            )
            self._event(
                run.run_id,
                "EVIDENCE_QUERY_FAILED",
                task_id=task.id,
                failure_code="diagnostic_unverified_result",
                retryable=False,
            )
            raise DiagnosticFailure("diagnostic_unverified_result")
        source = response.evidence.datasource_id
        if checkpoint.diagnostic_datasource_id and source != checkpoint.diagnostic_datasource_id:
            self.repository.save_task(
                task.model_copy(
                    update={
                        "status": TaskStatus.FAILED,
                        "ask_response_json": data,
                        "failure_code": "diagnostic_datasource_mismatch",
                        "retryable": False,
                    }
                )
            )
            self._event(
                run.run_id,
                "EVIDENCE_QUERY_FAILED",
                task_id=task.id,
                failure_code="diagnostic_datasource_mismatch",
                retryable=False,
            )
            raise DiagnosticFailure("diagnostic_datasource_mismatch", blocked=True)
        evidence_ref = f"evidence:{run.run_id}:{task.id}"
        self.repository.save_task(
            task.model_copy(
                update={
                    "status": TaskStatus.COMPLETED,
                    "ask_response_json": data,
                    "evidence_ref": evidence_ref,
                    "completed_at": now(),
                }
            )
        )
        checkpoint = self.repository.save_checkpoint(
            checkpoint.model_copy(
                update={
                    "diagnostic_datasource_id": source,
                    "pending_task_id": None,
                    "questions_completed": checkpoint.questions_completed + 1,
                    "verified_observation_refs": [
                        *checkpoint.verified_observation_refs,
                        evidence_ref,
                    ],
                }
            )
        )
        self._event(
            run.run_id,
            "EVIDENCE_QUERY_COMPLETED",
            task_id=task.id,
            evidence_ref=evidence_ref,
            datasource_id=source,
            row_count=response.result.row_count,
        )
        return checkpoint

    def drive(self, run, conversation, question: str, memory, clarifications: list[str]):
        checkpoint = self.repository.checkpoint(run.run_id)
        completed_tasks = [
            task
            for task in self.repository.tasks(run.run_id)
            if task.status == TaskStatus.COMPLETED
        ]
        refs = [task.evidence_ref for task in completed_tasks if task.evidence_ref]
        if (
            checkpoint.questions_completed != len(completed_tasks)
            or checkpoint.verified_observation_refs != refs
        ):
            source = checkpoint.diagnostic_datasource_id
            if refs and source is None:
                first = next(task for task in completed_tasks if task.evidence_ref)
                source = AskResponse.model_validate(first.ask_response_json).evidence.datasource_id
            checkpoint = self.repository.save_checkpoint(
                checkpoint.model_copy(
                    update={
                        "questions_completed": len(completed_tasks),
                        "verified_observation_refs": refs,
                        "diagnostic_datasource_id": source,
                    }
                )
            )
        planning_failed = False
        if checkpoint.current_round == 0 and checkpoint.remaining_budget == 0:
            checkpoint = self.repository.save_checkpoint(
                checkpoint.model_copy(
                    update={
                        "remaining_budget": self.budget.max_evidence_questions,
                        "diagnostic_datasource_id": (
                            conversation.datasource_scope[0]
                            if conversation.datasource_scope
                            else None
                        ),
                    }
                )
            )
            self._event(run.run_id, "DIAGNOSIS_STARTED")
        if self.model is None and not checkpoint.verified_observation_refs:
            raise DiagnosticFailure("diagnostic_model_unavailable", blocked=True)
        if (
            checkpoint.current_round == 0
            and not memory.confirmed_business_query
            and not clarifications
            and any(word in question for word in ("下降", "减少", "少了", "下滑"))
            and RuleExtractor().extract(question).comparison is None
            and not any(word in question for word in ("去年", "上个月", "上周", "同期", "之前"))
        ):
            self._wait_for_clarification(run, question, "请明确下降是与哪个时间段或业务基准比较？")
            return
        while checkpoint.current_round <= self.budget.max_adaptive_rounds:
            if self._cancelled(run):
                return
            tasks = [
                item
                for item in self.repository.tasks(run.run_id)
                if item.round == checkpoint.current_round
            ]
            if tasks and all(
                item.status in {TaskStatus.COMPLETED, TaskStatus.SKIPPED} for item in tasks
            ):
                tasks = []
            if not tasks:
                if (
                    checkpoint.current_round >= self.budget.max_adaptive_rounds
                    or checkpoint.remaining_budget <= 0
                ):
                    break
                observations = self._observations(run.run_id)
                safe_memory = _safe_value(
                    {
                        "active_metrics": memory.active_metrics,
                        "active_dimensions": memory.active_dimensions,
                        "active_filters": memory.active_filters,
                        "active_time_expression": memory.active_time_expression,
                        "diagnostic_findings": [
                            {
                                "target_summary": str(item.get("target_summary", ""))[:240],
                                "statement": str(item.get("statement", ""))[:500],
                                "evidence_refs": item.get("evidence_refs", [])[-3:],
                                "scan_version": item.get("scan_version"),
                                "observed_at": item.get("observed_at"),
                            }
                            for item in memory.diagnostic_findings[-3:]
                        ],
                    },
                    SecretRedactor.from_environment(),
                )
                context = DiagnosticPlanningContext(
                    question=question,
                    semantic_memory=safe_memory,
                    observations=observations,
                    round=checkpoint.current_round + 1,
                    remaining_budget=checkpoint.remaining_budget,
                )
                try:
                    decision = self.model.propose_evidence_questions(context)
                    seen = {
                        question_key(item.question) for item in self.repository.tasks(run.run_id)
                    }
                    selected, duplicates = select_questions(
                        decision,
                        seen,
                        checkpoint.remaining_budget,
                        self.budget,
                    )
                except Exception as error:
                    if any(item.status == "verified" for item in observations):
                        logger.warning("diagnostic planning stopped: %s", type(error).__name__)
                        planning_failed = True
                        break
                    raise DiagnosticFailure("diagnostic_model_unavailable", blocked=True) from error
                if decision.action == "clarify":
                    message = decision.clarification or "请明确诊断基准。"
                    self._wait_for_clarification(run, question, message)
                    return
                round_number = checkpoint.current_round + 1
                planned_tasks = []
                for index, item in enumerate([*selected, *duplicates], 1):
                    task = DiagnosticTask(
                        id=str(uuid4()),
                        run_id=run.run_id,
                        round=round_number,
                        ordinal=index,
                        question=item.question,
                        purpose=item.purpose,
                        status=(
                            TaskStatus.PLANNED if index <= len(selected) else TaskStatus.SKIPPED
                        ),
                    )
                    planned_tasks.append(task)
                checkpoint = self.repository.plan_round(checkpoint, planned_tasks)
                self._event(run.run_id, "DIAGNOSTIC_ROUND_STARTED", round=round_number)
                for task in planned_tasks:
                    if task.status == TaskStatus.PLANNED:
                        self._event(
                            run.run_id,
                            "EVIDENCE_QUESTION_PLANNED",
                            task_id=task.id,
                            question=task.question,
                            purpose=task.purpose,
                            round=round_number,
                        )
                if decision.action == "stop" or not selected:
                    break
                tasks = [
                    item for item in self.repository.tasks(run.run_id) if item.round == round_number
                ]
            for task in tasks:
                if self._cancelled(run):
                    return
                if task.status in {TaskStatus.COMPLETED, TaskStatus.SKIPPED}:
                    continue
                if task.status == TaskStatus.FAILED and not task.retryable:
                    raise DiagnosticFailure(task.failure_code or "diagnostic_task_failed")
                answer = (
                    clarifications[-1]
                    if task.status == TaskStatus.WAITING_USER and clarifications
                    else None
                )
                checkpoint = self._execute_task(run, task, conversation, checkpoint, answer)
                if self.runtime.runs.get(run.run_id).status == RunStatus.WAITING_USER:
                    return
            if checkpoint.current_round >= self.budget.max_adaptive_rounds:
                break
        observations = self._observations(run.run_id)
        verified = [item for item in observations if item.status == "verified"]
        if not verified:
            raise DiagnosticFailure("diagnostic_no_verified_evidence", blocked=True)
        if self._cancelled(run):
            return
        self._event(run.run_id, "DIAGNOSTIC_SYNTHESIS_STARTED", verified_count=len(verified))
        synthesis = DiagnosticSynthesisContext(
            question=question,
            observations=verified,
            unavailable=[item for item in observations if item.status == "unavailable"],
        )
        answer, source = (
            (deterministic_answer(synthesis), "diagnostic_deterministic_fallback")
            if planning_failed
            else self.composer.compose(synthesis, question, memory.model_dump(mode="json"))
        )
        if self._cancelled(run):
            return
        final = AskResponse(
            question=question,
            status=AskStatus.COMPLETED,
            answer=answer,
            analysis={
                "answer_source": source,
                "run_kind": "diagnostic",
                "verified_observations": len(verified),
                "evidence_refs": [item.evidence_ref for item in verified],
            },
        )
        current = self.runtime.runs.get(run.run_id)
        current = self.runtime._advance(current, RunStatus.ANSWERING)
        current = self.runtime._save(
            transition(
                current,
                RunStatus.COMPLETED,
                response_json=final.model_dump(mode="json"),
                completed_at=now(),
            )
        )
        self.runtime.messages.assistant_message(run.conversation_id, answer, run.run_id)
        self.record_memory(current, question, answer, memory, verified)
        self._event(run.run_id, "DIAGNOSIS_COMPLETED", verified_count=len(verified))
        self._event(run.run_id, "RUN_COMPLETED")

    def record_memory(self, run, question, answer, memory, verified=None):
        verified = (
            verified
            if verified is not None
            else [item for item in self._observations(run.run_id) if item.status == "verified"]
        )
        if not verified or run.status != RunStatus.COMPLETED:
            return
        if any(item.get("run_id") == run.run_id for item in memory.diagnostic_findings):
            return
        finding = DiagnosticFinding(
            finding_id=f"finding:{run.run_id}",
            run_id=run.run_id,
            target_summary=question[:240],
            statement=answer[:1000],
            metric=memory.active_metrics,
            dimensions=memory.active_dimensions,
            filters=memory.active_filters,
            evidence_refs=[item.evidence_ref for item in verified if item.evidence_ref],
            datasource_id=verified[0].datasource_id or "",
            scan_version=verified[0].scan_version,
        )
        updated = memory.model_copy(
            update={
                "diagnostic_findings": [
                    *memory.diagnostic_findings,
                    finding.model_dump(mode="json"),
                ],
                "updated_at": now(),
            }
        )
        self.runtime.conversations.put_memory(
            run.conversation_id,
            updated,
            self.runtime.conversations.memory_revision(run.conversation_id),
        )
