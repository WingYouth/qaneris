"""Conversation run execution over one existing Ask invocation per attempt."""

import re
from uuid import uuid4

from qaneris.application.inventory import is_workspace_inventory_question
from qaneris.capabilities.ask import AskCapability
from qaneris.common.errors import QanerisError
from qaneris.common.redaction import safe_error
from qaneris.contracts import AskClarification, AskRequest, AskResponse, AskStatus
from qaneris.contracts.semantic import BusinessObjective
from qaneris.conversation.context import ConversationContextResolver
from qaneris.conversation.memory import confirmed_memory
from qaneris.conversation.models import Message, now
from qaneris.conversation.ports import ConversationRepository
from qaneris.conversation.service import ConversationService
from qaneris.diagnostics.coordinator import DiagnosticCoordinator, DiagnosticFailure
from qaneris.diagnostics.repository import SQLiteDiagnosticRepository
from qaneris.federation.coordinator import FederatedQuestionCapability
from qaneris.federation.governance import FederationFailure
from qaneris.federation.repository import SQLiteFederationRepository
from qaneris.federation.router import FederationRouter, ready_scope
from qaneris.runtime.models import Run, RunStatus
from qaneris.runtime.ports import RunRepository, RunScheduler
from qaneris.runtime.state_machine import transition
from qaneris.semantic.intent import RuleExtractor

_ORDER = [
    RunStatus.CONTEXTUALIZING,
    RunStatus.DISCOVERING,
    RunStatus.PLANNING,
    RunStatus.EXECUTING,
    RunStatus.ANSWERING,
]
_TERMINAL = {
    RunStatus.COMPLETED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
    RunStatus.BLOCKED,
    RunStatus.WAITING_USER,
}
_DIAGNOSTIC_FOLLOWUP = re.compile(r"(?:再深入看看|深入分析|继续诊断|继续分析原因|再看看.*原因)")


class RunOrchestrator:
    def __init__(
        self,
        conversations: ConversationRepository,
        runs: RunRepository,
        ask: AskCapability,
        resolver: ConversationContextResolver,
        scheduler: RunScheduler,
        diagnostic_model=None,
        diagnostic_repository=None,
        federation_service=None,
        federation_repository=None,
        federation_model=None,
    ):
        self.conversations = conversations
        self.runs = runs
        self.ask = ask
        self.resolver = resolver
        self.scheduler = scheduler
        self.messages = ConversationService(conversations)
        candidate = diagnostic_model if diagnostic_model is not None else resolver.model
        model = (
            candidate
            if candidate is not None
            and all(
                hasattr(candidate, name)
                for name in ("propose_evidence_questions", "synthesize_diagnosis")
            )
            else None
        )
        repository = diagnostic_repository or SQLiteDiagnosticRepository(runs.path)
        self.diagnostics = DiagnosticCoordinator(self, repository, model)
        self.federation_service = federation_service
        self.federation_router = FederationRouter(federation_service) if federation_service else None
        self.federation = FederatedQuestionCapability(
            self,
            federation_repository or SQLiteFederationRepository(runs.path),
            federation_service,
            federation_model if federation_model is not None else resolver.model,
        )
        self.runs.recover_interrupted()
        self._recover_completions()
        self._recover_clarifications()
        for pending_run_id in self.runs.queued_run_ids():
            self.scheduler.submit(pending_run_id, self.execute)

    def _recover_completions(self) -> None:
        for run in self.runs.incomplete_completions():
            if run.response_json is None:
                continue
            if run.run_kind == "federated":
                self.messages.assistant_message(
                    run.conversation_id, run.response_json["answer"], run.run_id
                )
                plan = self.federation.repository.plan(run.run_id)
                memory = self.conversations.memory(run.conversation_id)
                if plan and memory.last_run_id != run.run_id:
                    from qaneris.federation.models import MergedResult
                    self.federation._record_memory(
                        run, plan,
                        MergedResult.model_validate(run.response_json["merged_result"]), memory,
                    )
                self.runs.append_event(run.run_id, "RUN_COMPLETED", {})
                continue
            response = AskResponse.model_validate(run.response_json)
            self.messages.assistant_message(run.conversation_id, response.answer, run.run_id)
            memory = self.conversations.memory(run.conversation_id)
            if run.run_kind == "diagnostic":
                if not any(item.get("run_id") == run.run_id for item in memory.diagnostic_findings):
                    self.diagnostics.record_memory(
                        run,
                        run.resolved_question or response.question,
                        response.answer,
                        memory,
                    )
                self.runs.append_event(run.run_id, "RUN_COMPLETED", {})
                continue
            if (
                response.business_query is not None
                and memory.last_run_id != run.run_id
                and run.completed_at is not None
                and memory.updated_at <= run.completed_at
            ):
                self.conversations.put_memory(
                    run.conversation_id,
                    confirmed_memory(response, run.run_id, memory),
                    self.conversations.memory_revision(run.conversation_id),
                )
            self.runs.append_event(run.run_id, "RUN_COMPLETED", {})

    def _recover_clarifications(self) -> None:
        for run in self.runs.incomplete_clarifications():
            if run.response_json is None:
                continue
            if run.run_kind == "federated":
                self.messages.assistant_message(
                    run.conversation_id, run.response_json["clarification"], run.run_id,
                    kind="clarification", checkpoint_revision=run.revision,
                )
                self.runs.append_event(run.run_id, "CLARIFICATION_REQUIRED", {})
                continue
            response = AskResponse.model_validate(run.response_json)
            clarification = (
                response.clarification[0].question if response.clarification else response.answer
            )
            self.messages.assistant_message(
                run.conversation_id,
                clarification,
                run.run_id,
                kind="clarification",
                checkpoint_revision=run.revision,
            )
            self.runs.append_event(run.run_id, "CLARIFICATION_REQUIRED", {})

    def create(
        self,
        conversation_id: str,
        question: str,
        max_rows: int = 200,
        client_request_id: str | None = None,
    ) -> Run:
        conversation = self.conversations.get(conversation_id)
        if conversation.status != "active" or not question.strip():
            raise ValueError("active conversation and non-empty question required")
        if not 1 <= max_rows <= 1000:
            raise ValueError("max_rows must be between 1 and 1000")
        run = Run(
            run_id=str(uuid4()),
            conversation_id=conversation_id,
            user_message_id=str(uuid4()),
            workspace_id=conversation.workspace_id,
            max_rows=max_rows,
            client_request_id=client_request_id,
        )
        created = self.runs.create(run)
        if created.run_id == run.run_id:
            self.conversations.add_message(
                Message(
                    message_id=run.user_message_id,
                    conversation_id=conversation_id,
                    role="user",
                    content=question,
                    run_id=run.run_id,
                )
            )
            self.scheduler.submit(run.run_id, self.execute)
        return created

    def _save(self, run: Run) -> Run:
        return self.runs.save(run, run.revision)

    def _advance(self, run: Run, target: RunStatus) -> Run:
        current = run.status
        if current == target or current not in _ORDER or target not in _ORDER:
            return run
        for stage in _ORDER[_ORDER.index(current) + 1 : _ORDER.index(target) + 1]:
            run = self._save(transition(run, stage))
        return run

    def _cancel_if_requested(self, run: Run) -> Run | None:
        latest = self.runs.get(run.run_id)
        if not latest.cancel_requested:
            return None
        if latest.status in _TERMINAL:
            return latest
        cancelled = self._save(transition(latest, RunStatus.CANCELLED, completed_at=now()))
        self.runs.append_event(run.run_id, "RUN_CANCELLED", {})
        return cancelled

    def execute(self, run_id: str) -> None:
        run = self.runs.get(run_id)
        previous_clarification = None
        if (
            run.status == RunStatus.WAITING_USER
            and run.run_kind == "normal"
            and run.response_json
        ):
            previous = AskResponse.model_validate(run.response_json)
            if previous.clarification:
                previous_clarification = previous.clarification[0].question
        if (
            run.status == RunStatus.CREATED
            or run.status == RunStatus.WAITING_USER
            and run.current_stage == "resume_queued"
            or run.status == RunStatus.FAILED
            and run.current_stage == "retry_queued"
        ):
            pass
        else:
            return
        try:
            changes = {
                "failure_code": None,
                "failure_message": None,
                "retryable": False,
                "response_json": None,
            }
            if run.status == RunStatus.FAILED:
                changes["attempt"] = run.attempt + 1
            run = self._save(transition(run, RunStatus.CONTEXTUALIZING, **changes))
        except ValueError:  # another worker won the compare-and-swap
            return
        self.runs.append_event(run_id, "RUN_STARTED", {"attempt": run.attempt})
        try:
            self._drive(run, previous_clarification)
        except Exception as error:  # noqa: BLE001 - persist worker failure; never lose a run
            current = self.runs.get(run_id)
            if current.status == RunStatus.COMPLETED:
                self._recover_completions()
                return
            if current.status == RunStatus.WAITING_USER:
                self._recover_clarifications()
                return
            if self._cancel_if_requested(current):
                return
            if current.status in _TERMINAL:
                return
            code = (
                str(error)
                if str(error)
                in {
                    "conversation_context_model_unavailable",
                    "context_rule_preservation_failed",
                    "invalid_context_resolution",
                    "multi_source_scope_requires_iq05",
                }
                else (getattr(error, "code", "run_failed"))
            )
            blocked = (
                code
                in {
                    "conversation_context_model_unavailable",
                    "multi_source_scope_requires_iq05",
                }
                or isinstance(error, DiagnosticFailure)
                and error.blocked
                or isinstance(error, FederationFailure)
                and error.blocked
            )
            retryable = (
                code
                in {
                    "model_invocation_failed",
                    "graph_unavailable",
                    "datasource_unavailable",
                    "timeout",
                    "temporary_unavailable",
                    "rate_limited",
                    "transient_network",
                }
                or isinstance(error, DiagnosticFailure)
                and error.retryable
                or isinstance(error, FederationFailure)
                and error.retryable
            )
            target = RunStatus.BLOCKED if blocked else RunStatus.FAILED
            message = safe_error(error)[:240] if isinstance(error, QanerisError) else code
            if code == "unconfirmed_mapping":
                message = "当前数据对象没有已确认的跨数据源关联键，不会根据同名字段自动关联。"
            elif code == "stale_join_mapping":
                message = "跨数据源关联键绑定的扫描版本已变化，请重新确认映射。"
            elif code == "source_selection_ambiguous":
                message = "当前范围内有多个可用数据源，问题未指定使用哪一个。请在问题中写明数据源名称，或只选择一个数据源新建对话；跨库分析需明确各数据源及其业务口径。"
            elif code == "source_selection_unresolved":
                message = "确认内容没有选出唯一数据源。请新建对话并只选择一个数据源，或在问题中写明数据源名称。"
            elif code == "federation_semantics_missing":
                message = "所选数据源没有已发布的业务指标或维度，无法确认跨源分析口径。请先为参与的数据源发布相应业务定义。"
            current = self._save(
                transition(
                    current,
                    target,
                    failure_code=code,
                    failure_message=message,
                    retryable=retryable,
                    completed_at=now(),
                )
            )
            self.runs.append_event(
                run_id,
                "RUN_BLOCKED" if blocked else "RUN_FAILED",
                {"failure_code": code, "retryable": retryable},
            )

    def _drive(self, run: Run, previous_clarification: str | None = None) -> None:
        if self._cancel_if_requested(run):
            return
        conversation = self.conversations.get(run.conversation_id)
        messages = self.conversations.messages(run.conversation_id)
        original = next(item.content for item in messages if item.message_id == run.user_message_id)
        clarifications = [
            item.content
            for item in messages
            if item.run_id == run.run_id
            and item.message_kind == "clarification"
            and item.role == "user"
        ]
        question = f"{original}。用户澄清：{clarifications[-1]}" if clarifications else original
        memory = self.conversations.memory(run.conversation_id)
        resolved = self.resolver.resolve(question, memory, messages)
        diagnostic = run.run_kind == "diagnostic" or (
            RuleExtractor().extract(resolved).objective_hint == BusinessObjective.DIAGNOSIS
            or bool(memory.diagnostic_findings and _DIAGNOSTIC_FOLLOWUP.search(original))
        )
        run = self._save(
            run.model_copy(
                update={
                    "resolved_question": resolved,
                    "run_kind": (
                        "diagnostic" if diagnostic else
                        "federated" if run.run_kind == "federated" else "normal"
                    ),
                }
            )
        )
        self.runs.append_event(run.run_id, "CONTEXT_READY", {"resolved_question": resolved})
        if self._cancel_if_requested(run):
            return
        run = self._save(transition(run, RunStatus.DISCOVERING))
        if diagnostic:
            if len(conversation.datasource_scope) > 1:
                raise FederationFailure("diagnostic_requires_single_source", blocked=True)
            self.diagnostics.drive(run, conversation, resolved, memory, clarifications)
            return
        selected_datasource = (
            conversation.datasource_scope[0] if len(conversation.datasource_scope) == 1 else None
        )
        workspace_inventory = (
            not conversation.datasource_scope
            and is_workspace_inventory_question(resolved)
        )
        if self.federation_router and not workspace_inventory and (
            len(conversation.datasource_scope) != 1 or run.run_kind == "federated"
        ):
            allowed = ready_scope(
                self.federation_service, conversation.workspace_id,
                conversation.datasource_scope,
            )
            if run.run_kind == "federated" and self.federation.repository.plan(run.run_id):
                from qaneris.federation.router import RoutingDecision
                decision = RoutingDecision("federated", {}, [])
            else:
                try:
                    decision = self.federation_router.route(resolved, conversation.workspace_id, allowed)
                except FederationFailure as error:
                    if error.code != "source_selection_ambiguous":
                        raise
                    prompt = "请选择这次查询要使用的数据源。若要联合分析，请在问题中写明两个数据源。"
                    if previous_clarification == prompt:
                        raise FederationFailure("source_selection_unresolved", blocked=True) from error
                    names = [source.name for source in self.federation_service.list_datasources(
                        conversation.workspace_id
                    ) if source.id in allowed]
                    response = AskResponse(
                        question=resolved,
                        status=AskStatus.CLARIFICATION_REQUIRED,
                        answer=prompt,
                        clarification=[AskClarification(question=prompt, options=names[:20])],
                    )
                    run = self._save(transition(
                        run, RunStatus.WAITING_USER,
                        response_json=response.model_dump(mode="json"),
                    ))
                    self.messages.assistant_message(
                        run.conversation_id, prompt, run.run_id,
                        kind="clarification", checkpoint_revision=run.revision,
                    )
                    self.runs.append_event(run.run_id, "CLARIFICATION_REQUIRED", {})
                    return
            if decision.kind == "federated":
                run = self._save(run.model_copy(update={"run_kind": "federated"}))
                self.federation.drive(run, resolved, memory, allowed, decision, clarifications)
                return
            selected_datasource = decision.single_datasource_id
        elif len(conversation.datasource_scope) > 1:
            raise RuntimeError("multi_source_scope_requires_iq05")
        request = AskRequest(
            question=resolved,
            workspace_id=conversation.workspace_id,
            datasource_id=selected_datasource,
            max_rows=run.max_rows,
        )
        response: AskResponse | None = None
        failure: Exception | None = None
        # This is the only Ask invocation in this attempt. SSE reads persisted events only.
        records = iter(self.ask.execute(request))
        while True:
            if self._cancel_if_requested(run):
                close = getattr(records, "close", None)
                if close:
                    close()
                return
            try:
                record = next(records)
            except StopIteration:
                break
            if self._cancel_if_requested(run):
                close = getattr(records, "close", None)
                if close:
                    close()
                return
            if record.event.event_type == "plan_ready":
                run = self._advance(run, RunStatus.PLANNING)
            elif record.event.event_type == "execution_started":
                run = self._advance(run, RunStatus.EXECUTING)
            elif record.event.event_type == "result_ready":
                run = self._advance(run, RunStatus.ANSWERING)
            self.runs.append_event(
                run.run_id,
                "ASK_PROGRESS",
                {
                    "ask_event_type": record.event.event_type.value,
                    "ask_sequence": record.event.sequence,
                    "ask_payload": record.event.payload,
                },
            )
            if record.response is not None:
                response = record.response
            if record.exception is not None:
                failure = record.exception
        if self._cancel_if_requested(run):
            return
        if failure is not None:
            raise failure
        if response is None:
            raise RuntimeError("Ask ended without a response")
        if response.status == AskStatus.CLARIFICATION_REQUIRED:
            clarification = (
                response.clarification[0].question if response.clarification else response.answer
            )
            if response.clarification and not response.clarification[0].actionable:
                code = "clarification_requires_external_action"
                message = clarification
            elif previous_clarification == clarification:
                code = "clarification_unresolved"
                message = f"确认内容未能解决当前问题：{clarification}"
            else:
                code = None
                message = None
            if code:
                self._save(
                    transition(
                        run,
                        RunStatus.BLOCKED,
                        failure_code=code,
                        failure_message=message,
                        completed_at=now(),
                    )
                )
                self.runs.append_event(run.run_id, "RUN_BLOCKED", {"failure_code": code})
                return
            run = self._save(
                transition(
                    run, RunStatus.WAITING_USER, response_json=response.model_dump(mode="json")
                )
            )
            self.messages.assistant_message(
                run.conversation_id,
                clarification,
                run.run_id,
                kind="clarification",
                checkpoint_revision=run.revision,
            )
            self.runs.append_event(run.run_id, "CLARIFICATION_REQUIRED", {})
            return
        if response.status != AskStatus.COMPLETED:
            raise RuntimeError(response.error.code if response.error else "ask_failed")
        run = self._advance(run, RunStatus.ANSWERING)
        run = self._save(
            transition(
                run,
                RunStatus.COMPLETED,
                response_json=response.model_dump(mode="json"),
                completed_at=now(),
            )
        )
        self.messages.assistant_message(run.conversation_id, response.answer, run.run_id)
        if response.business_query is not None:
            self.conversations.put_memory(
                run.conversation_id,
                confirmed_memory(
                    response, run.run_id, self.conversations.memory(run.conversation_id)
                ),
                self.conversations.memory_revision(run.conversation_id),
            )
        self.runs.append_event(run.run_id, "RUN_COMPLETED", {})

    def clarify(self, run_id: str, answer: str) -> Run:
        if not answer.strip():
            raise ValueError("clarification answer is required")
        run = self.runs.get(run_id)
        if run.status != RunStatus.WAITING_USER or run.current_stage != "waiting_user":
            raise ValueError("run is not waiting for clarification")
        queued = self._save(run.model_copy(update={"current_stage": "resume_queued"}))
        self.messages.user_message(run.conversation_id, answer, run_id, "clarification")
        self.scheduler.submit(run_id, self.execute)
        return queued

    def cancel(self, run_id: str) -> Run:
        run = self.runs.get(run_id)
        if run.status in _TERMINAL:
            if run.status == RunStatus.WAITING_USER:
                cancelled = self._save(transition(run, RunStatus.CANCELLED, completed_at=now()))
                self.runs.append_event(run_id, "RUN_CANCELLED", {})
                return cancelled
            return run
        return self._save(run.model_copy(update={"cancel_requested": True}))

    def retry(self, run_id: str) -> Run:
        run = self.runs.get(run_id)
        if (
            run.status != RunStatus.FAILED
            or not run.retryable
            or run.current_stage == "retry_queued"
        ):
            raise ValueError("run is not retryable")
        queued = self._save(run.model_copy(update={"current_stage": "retry_queued"}))
        self.scheduler.submit(run_id, self.execute)
        return queued
