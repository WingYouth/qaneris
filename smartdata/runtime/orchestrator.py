"""Conversation run execution over one existing Ask invocation per attempt."""

from uuid import uuid4

from smartdata.capabilities.ask import AskCapability
from smartdata.common.errors import SmartDataError
from smartdata.common.redaction import safe_error
from smartdata.contracts import AskRequest, AskResponse, AskStatus
from smartdata.conversation.context import ConversationContextResolver
from smartdata.conversation.memory import confirmed_memory
from smartdata.conversation.models import Message, now
from smartdata.conversation.ports import ConversationRepository
from smartdata.conversation.service import ConversationService
from smartdata.runtime.models import Run, RunStatus
from smartdata.runtime.ports import RunRepository, RunScheduler
from smartdata.runtime.state_machine import transition

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


class RunOrchestrator:
    def __init__(
        self,
        conversations: ConversationRepository,
        runs: RunRepository,
        ask: AskCapability,
        resolver: ConversationContextResolver,
        scheduler: RunScheduler,
    ):
        self.conversations = conversations
        self.runs = runs
        self.ask = ask
        self.resolver = resolver
        self.scheduler = scheduler
        self.messages = ConversationService(conversations)
        self.runs.recover_interrupted()
        self._recover_completions()
        self._recover_clarifications()
        for pending_run_id in self.runs.queued_run_ids():
            self.scheduler.submit(pending_run_id, self.execute)

    def _recover_completions(self) -> None:
        for run in self.runs.incomplete_completions():
            if run.response_json is None:
                continue
            response = AskResponse.model_validate(run.response_json)
            self.messages.assistant_message(run.conversation_id, response.answer, run.run_id)
            memory = self.conversations.memory(run.conversation_id)
            if (
                response.business_query is not None
                and memory.last_run_id != run.run_id
                and run.completed_at is not None
                and memory.updated_at <= run.completed_at
            ):
                self.conversations.put_memory(
                    run.conversation_id,
                    confirmed_memory(response, run.run_id),
                    self.conversations.memory_revision(run.conversation_id),
                )
            self.runs.append_event(run.run_id, "RUN_COMPLETED", {})

    def _recover_clarifications(self) -> None:
        for run in self.runs.incomplete_clarifications():
            if run.response_json is None:
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
            self._drive(run)
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
            blocked = code in {
                "conversation_context_model_unavailable",
                "multi_source_scope_requires_iq05",
            }
            retryable = code in {
                "model_invocation_failed",
                "graph_unavailable",
                "datasource_unavailable",
                "timeout",
                "temporary_unavailable",
                "rate_limited",
                "transient_network",
            }
            target = RunStatus.BLOCKED if blocked else RunStatus.FAILED
            message = safe_error(error)[:240] if isinstance(error, SmartDataError) else code
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

    def _drive(self, run: Run) -> None:
        if self._cancel_if_requested(run):
            return
        conversation = self.conversations.get(run.conversation_id)
        if len(conversation.datasource_scope) > 1:
            raise RuntimeError("multi_source_scope_requires_iq05")
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
        run = self._save(run.model_copy(update={"resolved_question": resolved}))
        self.runs.append_event(run.run_id, "CONTEXT_READY", {"resolved_question": resolved})
        if self._cancel_if_requested(run):
            return
        run = self._save(transition(run, RunStatus.DISCOVERING))
        request = AskRequest(
            question=resolved,
            workspace_id=conversation.workspace_id,
            datasource_id=(
                conversation.datasource_scope[0] if conversation.datasource_scope else None
            ),
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
            run = self._save(
                transition(
                    run, RunStatus.WAITING_USER, response_json=response.model_dump(mode="json")
                )
            )
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
                confirmed_memory(response, run.run_id),
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
