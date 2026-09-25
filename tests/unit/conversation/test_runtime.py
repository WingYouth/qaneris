"""Durable conversation behavior at the Ask capability boundary."""

from dataclasses import dataclass
from pathlib import Path

import pytest

from qaneris.common.errors import ModelInvocationError
from qaneris.contracts import AskClarification, AskEvent, AskResponse, AskStatus
from qaneris.contracts.semantic import BusinessFilter, BusinessQuery, ComparisonSpec
from qaneris.conversation.context import ConversationContextResolver, working_set
from qaneris.conversation.models import Message, SemanticMemory, now
from qaneris.conversation.repository import SQLiteConversationRepository
from qaneris.conversation.service import ConversationService
from qaneris.runtime.models import Run, RunStatus
from qaneris.runtime.orchestrator import RunOrchestrator
from qaneris.runtime.repository import SQLiteRunRepository
from qaneris.runtime.scheduler import InlineRunScheduler
from qaneris.runtime.state_machine import transition


@dataclass
class Record:
    event: AskEvent
    response: AskResponse | None = None
    exception: Exception | None = None


class FakeModel:
    def resolve_followup(self, question, context):
        memory = context["confirmed_semantic_memory"]
        metric = memory["active_metrics"][0]
        if "按地区" in question:
            return f"今年{metric}按地区看"
        if "华东" in question:
            return f"今年{metric}按地区看，只看华东"
        return f"今年{metric}按地区只看华东，跟去年比"


class FakeAsk:
    def __init__(self):
        self.questions = []

    def execute(self, request):
        self.questions.append(request.question)
        question = request.question
        filters = (
            [BusinessFilter(subject="地区", operator="=", value="华东")]
            if "华东" in question
            else []
        )
        query = BusinessQuery(
            question=question,
            objective="comparison" if "去年" in question else "lookup",
            metrics=["销售额"],
            dimensions=["地区"] if "地区" in question else [],
            time_expression="今年",
            filters=filters,
            comparison=(
                ComparisonSpec(comparison_type="year_over_year") if "去年" in question else None
            ),
        )
        response = AskResponse(
            question=question,
            status=AskStatus.COMPLETED,
            answer="销售额为 100",
            business_query=query,
        )
        for index, kind in enumerate(
            ("accepted", "plan_ready", "execution_started", "result_ready", "done"), 1
        ):
            yield Record(
                AskEvent(event_type=kind, sequence=index, correlation_id="fake"),
                response if kind == "done" else None,
            )


def build(tmp_path: Path, ask=None, model=None, scheduler=None):
    path = str(tmp_path / "runtime.db")
    conversations = SQLiteConversationRepository(path)
    runs = SQLiteRunRepository(path)
    ask = ask or FakeAsk()
    runtime = RunOrchestrator(
        conversations,
        runs,
        ask,
        ConversationContextResolver(model or FakeModel()),
        scheduler or InlineRunScheduler(),
    )
    return conversations, runs, ask, runtime


def test_four_turn_memory_reopen_and_single_execution(tmp_path):
    conversations, runs, ask, runtime = build(tmp_path)
    conversation = ConversationService(conversations).create()
    questions = ["今年销售额是多少？", "那按地区看呢？", "只看华东。", "跟去年比呢？"]
    for question in questions:
        run = runtime.create(conversation.conversation_id, question)
        assert runs.get(run.run_id).status == RunStatus.COMPLETED
    assert len(ask.questions) == 4
    assert "地区" in ask.questions[1]
    assert "华东" in ask.questions[2]
    assert "去年" in ask.questions[3]
    reopened = SQLiteConversationRepository(str(tmp_path / "runtime.db"))
    assert len(reopened.messages(conversation.conversation_id)) == 8
    memory = reopened.memory(conversation.conversation_id)
    assert memory.active_metrics == ["销售额"]
    assert memory.active_filters[0]["value"] == "华东"
    assert memory.confirmed_business_query["comparison"]["comparison_type"] == "year_over_year"
    assert memory.last_run_id == run.run_id
    assert [event.sequence for event in runs.events(run.run_id, 3)] == list(
        range(4, len(runs.events(run.run_id)) + 1)
    )


def test_new_standalone_question_does_not_inherit_memory(tmp_path):
    conversations, _, ask, runtime = build(tmp_path)
    conversation = ConversationService(conversations).create()
    runtime.create(conversation.conversation_id, "今年销售额是多少？")
    runtime.create(conversation.conversation_id, "最近30天订单量是多少？")
    assert ask.questions[-1] == "最近30天订单量是多少？"


def test_rule_preservation_blocks_ask(tmp_path):
    class Tamper:
        def resolve_followup(self, question, context):
            return "2026 年 7 月，金额 > 1000"

    conversations, runs, ask, runtime = build(tmp_path, model=Tamper())
    conversation = ConversationService(conversations).create()
    runtime.create(conversation.conversation_id, "今年销售额是多少？")
    run = runtime.create(conversation.conversation_id, "只看 2026 年 8 月，金额 > 100")
    assert runs.get(run.run_id).status == RunStatus.FAILED
    assert runs.get(run.run_id).failure_code == "context_rule_preservation_failed"
    assert len(ask.questions) == 1


def test_context_model_cannot_invent_datasource_id():
    class Invent:
        def resolve_followup(self, question, context):
            return "按地区看销售额，数据源 ds-other"

    resolver = ConversationContextResolver(Invent())
    memory = SemanticMemory(
        confirmed_business_query={"question": "销售额"}, active_metrics=["销售额"]
    )
    with pytest.raises(ValueError, match="context_rule_preservation_failed"):
        resolver.resolve("那按地区看呢？", memory, [])


def test_message_and_title_redact_credential_text(tmp_path):
    conversations, _, _, runtime = build(tmp_path)
    conversation = ConversationService(conversations).create(title="password=hidden-secret")
    runtime.create(conversation.conversation_id, "销售额是多少？ token=hidden-secret")
    reopened = SQLiteConversationRepository(str(tmp_path / "runtime.db"))
    assert "hidden-secret" not in reopened.get(conversation.conversation_id).title
    assert "hidden-secret" not in reopened.messages(conversation.conversation_id)[0].content


def test_repository_cas_idempotency_and_recovery(tmp_path):
    conversations, runs, _, runtime = build(tmp_path)
    conversation = ConversationService(conversations).create()
    run = Run(
        run_id="one",
        conversation_id=conversation.conversation_id,
        user_message_id="message",
        workspace_id="default",
        client_request_id="key",
    )
    runs.create(run)
    assert runs.create(run.model_copy(update={"run_id": "other"})).run_id == "one"
    active = runs.save(transition(run, RunStatus.CONTEXTUALIZING), 0)
    with pytest.raises(ValueError, match="revision conflict"):
        runs.save(transition(run, RunStatus.CONTEXTUALIZING), 0)
    runs.append_event("one", "RUN_STARTED", {})
    runs.append_event("one", "CONTEXT_READY", {})
    assert [event.sequence for event in runs.events("one", 1)] == [2]
    SQLiteRunRepository(str(tmp_path / "runtime.db")).recover_interrupted()
    recovered = runs.get("one")
    assert recovered.status == RunStatus.FAILED
    assert recovered.retryable and recovered.failure_code == "run_interrupted"
    assert active.revision == 1
    assert runtime.runs.get("one").attempt == 1


def test_public_event_store_redacts_private_payload(tmp_path):
    _, runs, _, _ = build(tmp_path)
    run = Run(run_id="safe", conversation_id="c", user_message_id="m", workspace_id="default")
    runs.create(run)
    runs.append_event(
        "safe",
        "ASK_PROGRESS",
        {
            "reasoning_content": "private",
            "password": "secret-value",
            "parameters": ["bound-value"],
            "stage": "execution",
        },
    )
    event = SQLiteRunRepository(str(tmp_path / "runtime.db")).events("safe")[0]
    assert "reasoning_content" not in event.public_payload
    assert event.public_payload["password"] == "<redacted>"
    assert event.public_payload["parameters"] == "<redacted>"


def test_state_machine_rejects_terminal_transition():
    run = Run(
        run_id="r",
        conversation_id="c",
        user_message_id="m",
        workspace_id="default",
        status=RunStatus.COMPLETED,
    )
    with pytest.raises(ValueError):
        transition(run, RunStatus.CONTEXTUALIZING)


@pytest.mark.parametrize(
    "source,target",
    [
        (RunStatus.CREATED, RunStatus.CONTEXTUALIZING),
        (RunStatus.CONTEXTUALIZING, RunStatus.DISCOVERING),
        (RunStatus.DISCOVERING, RunStatus.PLANNING),
        (RunStatus.DISCOVERING, RunStatus.WAITING_USER),
        (RunStatus.DISCOVERING, RunStatus.FAILED),
        (RunStatus.PLANNING, RunStatus.EXECUTING),
        (RunStatus.EXECUTING, RunStatus.ANSWERING),
        (RunStatus.EXECUTING, RunStatus.FAILED),
        (RunStatus.EXECUTING, RunStatus.CANCELLED),
        (RunStatus.ANSWERING, RunStatus.COMPLETED),
        (RunStatus.WAITING_USER, RunStatus.CONTEXTUALIZING),
        (RunStatus.FAILED, RunStatus.CONTEXTUALIZING),
    ],
)
def test_formal_state_transitions(source, target):
    run = Run(
        run_id="r",
        conversation_id="c",
        user_message_id="m",
        workspace_id="default",
        status=source,
        retryable=source == RunStatus.FAILED,
    )
    assert transition(run, target).status == target


def test_working_set_is_bounded_and_deterministic():
    messages = [
        Message(message_id=str(index), conversation_id="c", role="user", content="x" * 100)
        for index in range(300)
    ]
    memory = SemanticMemory(
        confirmed_business_query={"question": "销售额"}, active_metrics=["销售额"]
    )
    first = working_set(messages, memory, max_messages=6, max_chars=500)
    second = working_set(messages, memory, max_messages=6, max_chars=500)
    assert first == second
    assert len(first["messages"]) <= 6
    assert sum(len(item["content"]) for item in first["messages"]) < 500


def test_unavailable_model_blocks_elliptical_followup(tmp_path):
    conversations, runs, ask, runtime = build(tmp_path)
    conversation = ConversationService(conversations).create()
    runtime.create(conversation.conversation_id, "今年销售额是多少？")
    runtime.resolver = ConversationContextResolver(None)
    run = runtime.create(conversation.conversation_id, "那按地区看呢？")
    assert runs.get(run.run_id).status == RunStatus.BLOCKED
    assert len(ask.questions) == 1
    assert isinstance(conversations.memory(conversation.conversation_id), SemanticMemory)


class QueuedScheduler:
    def __init__(self):
        self.jobs = []

    def submit(self, run_id, work):
        self.jobs.append((run_id, work))

    def drain(self):
        for run_id, work in self.jobs:
            work(run_id)
        self.jobs.clear()


def test_duplicate_submission_and_cancel_do_not_execute_ask(tmp_path):
    scheduler = QueuedScheduler()
    conversations, runs, ask, runtime = build(tmp_path, scheduler=scheduler)
    conversation = ConversationService(conversations).create()
    run = runtime.create(conversation.conversation_id, "销售额是多少？")
    scheduler.submit(run.run_id, runtime.execute)
    scheduler.drain()
    assert len(ask.questions) == 1
    assert runs.get(run.run_id).status == RunStatus.COMPLETED
    cancelled = runtime.create(conversation.conversation_id, "订单数是多少？")
    runtime.cancel(cancelled.run_id)
    scheduler.drain()
    assert runs.get(cancelled.run_id).status == RunStatus.CANCELLED
    assert len(ask.questions) == 1
    assert conversations.memory(conversation.conversation_id).last_run_id == run.run_id


def test_cancel_at_ask_stage_boundary_keeps_memory_unconfirmed(tmp_path):
    class InterruptingAsk:
        runtime = None
        run_id = None
        calls = 0

        def execute(self, request):
            self.calls += 1
            yield Record(AskEvent(event_type="accepted", sequence=1, correlation_id="fake"))
            self.runtime.cancel(self.run_id)
            yield Record(AskEvent(event_type="plan_ready", sequence=2, correlation_id="fake"))

    scheduler = QueuedScheduler()
    ask = InterruptingAsk()
    conversations, runs, _, runtime = build(tmp_path, ask=ask, scheduler=scheduler)
    conversation = ConversationService(conversations).create()
    run = runtime.create(conversation.conversation_id, "销售额是多少？")
    ask.runtime, ask.run_id = runtime, run.run_id
    scheduler.drain()
    assert runs.get(run.run_id).status == RunStatus.CANCELLED
    assert conversations.memory(conversation.conversation_id).last_run_id is None
    assert ask.calls == 1


def test_clarification_resumes_same_run_and_commits_only_after_completion(tmp_path):
    class ClarifyingAsk(FakeAsk):
        def execute(self, request):
            if "用户澄清" not in request.question:
                self.questions.append(request.question)
                response = AskResponse(
                    question=request.question,
                    status=AskStatus.CLARIFICATION_REQUIRED,
                    clarification=[AskClarification(question="选择销售额口径")],
                )
                yield Record(
                    AskEvent(event_type="done", sequence=1, correlation_id="fake"), response
                )
            else:
                yield from super().execute(request)

    conversations, runs, ask, runtime = build(tmp_path, ask=ClarifyingAsk())
    conversation = ConversationService(conversations).create()
    run = runtime.create(conversation.conversation_id, "销售额是多少？")
    assert runs.get(run.run_id).status == RunStatus.WAITING_USER
    assert conversations.memory(conversation.conversation_id).confirmed_business_query is None
    runtime.clarify(run.run_id, "实收销售额")
    assert runs.get(run.run_id).status == RunStatus.COMPLETED
    assert runs.get(run.run_id).attempt == 1
    assert "实收销售额" in ask.questions[-1]
    assert conversations.memory(conversation.conversation_id).last_run_id == run.run_id


def test_same_run_retry_after_dependency_failure(tmp_path):
    class FlakyAsk(FakeAsk):
        def execute(self, request):
            if not self.questions:
                self.questions.append(request.question)
                raise ModelInvocationError("temporary")
            yield from super().execute(request)

    conversations, runs, ask, runtime = build(tmp_path, ask=FlakyAsk())
    conversation = ConversationService(conversations).create()
    run = runtime.create(conversation.conversation_id, "销售额是多少？")
    assert runs.get(run.run_id).status == RunStatus.FAILED
    assert runs.get(run.run_id).retryable
    assert conversations.memory(conversation.conversation_id).last_run_id is None
    runtime.retry(run.run_id)
    assert runs.get(run.run_id).status == RunStatus.COMPLETED
    assert runs.get(run.run_id).attempt == 2
    assert len(ask.questions) == 2


def test_non_retryable_failure_refuses_retry(tmp_path):
    scheduler = QueuedScheduler()
    conversations, runs, _, runtime = build(tmp_path, scheduler=scheduler)
    conversation = ConversationService(conversations).create()
    run = runtime.create(conversation.conversation_id, "销售额是多少？")
    run = runs.save(transition(run, RunStatus.CONTEXTUALIZING), run.revision)
    runs.save(transition(run, RunStatus.FAILED, retryable=False), run.revision)
    with pytest.raises(ValueError, match="not retryable"):
        runtime.retry(run.run_id)


def test_idempotent_create_does_not_add_message_or_execute_again(tmp_path):
    conversations, runs, ask, runtime = build(tmp_path)
    conversation = ConversationService(conversations).create()
    first = runtime.create(
        conversation.conversation_id, "销售额是多少？", client_request_id="request-1"
    )
    second = runtime.create(
        conversation.conversation_id, "销售额是多少？", client_request_id="request-1"
    )
    assert first.run_id == second.run_id
    assert len(conversations.messages(conversation.conversation_id)) == 2
    assert len(ask.questions) == 1
    assert runs.get(first.run_id).revision > 0


def test_multi_source_scope_blocks_without_widening(tmp_path):
    conversations, runs, ask, runtime = build(tmp_path)
    conversation = ConversationService(conversations).create(
        datasource_ids=["source-a", "source-b"]
    )
    run = runtime.create(conversation.conversation_id, "销售额是多少？")
    assert runs.get(run.run_id).status == RunStatus.BLOCKED
    assert runs.get(run.run_id).failure_code == "multi_source_scope_requires_iq05"
    assert not ask.questions


def test_standalone_question_runs_without_context_model(tmp_path):
    conversations, runs, ask, runtime = build(tmp_path)
    runtime.resolver = ConversationContextResolver(None)
    conversation = ConversationService(conversations).create()
    run = runtime.create(conversation.conversation_id, "销售额是多少？")
    assert runs.get(run.run_id).status == RunStatus.COMPLETED
    assert len(ask.questions) == 1


def test_queued_clarification_survives_restart(tmp_path):
    class ClarifyingAsk(FakeAsk):
        def execute(self, request):
            if "用户澄清" not in request.question:
                self.questions.append(request.question)
                yield Record(
                    AskEvent(event_type="done", sequence=1, correlation_id="fake"),
                    AskResponse(
                        question=request.question,
                        status=AskStatus.CLARIFICATION_REQUIRED,
                        clarification=[AskClarification(question="哪个口径？")],
                    ),
                )
            else:
                yield from super().execute(request)

    ask = ClarifyingAsk()
    conversations, _runs, _, runtime = build(tmp_path, ask=ask)
    conversation = ConversationService(conversations).create()
    run = runtime.create(conversation.conversation_id, "销售额是多少？")
    paused = QueuedScheduler()
    runtime.scheduler = paused
    runtime.clarify(run.run_id, "实收销售额")
    restarted = RunOrchestrator(
        SQLiteConversationRepository(str(tmp_path / "runtime.db")),
        SQLiteRunRepository(str(tmp_path / "runtime.db")),
        ask,
        ConversationContextResolver(FakeModel()),
        InlineRunScheduler(),
    )
    assert restarted.runs.get(run.run_id).status == RunStatus.COMPLETED
    assert len(ask.questions) == 2


def test_restart_repairs_completed_run_terminalization_once(tmp_path):
    scheduler = QueuedScheduler()
    conversations, runs, ask, runtime = build(tmp_path, scheduler=scheduler)
    conversation = ConversationService(conversations).create()
    run = runtime.create(conversation.conversation_id, "销售额是多少？")
    response = AskResponse(
        question="销售额是多少？",
        answer="100",
        business_query=BusinessQuery(
            question="销售额是多少？", objective="lookup", metrics=["销售额"]
        ),
    )
    for stage in (
        RunStatus.CONTEXTUALIZING,
        RunStatus.DISCOVERING,
        RunStatus.PLANNING,
        RunStatus.EXECUTING,
        RunStatus.ANSWERING,
    ):
        run = runs.save(transition(run, stage), run.revision)
    runs.save(
        transition(
            run,
            RunStatus.COMPLETED,
            response_json=response.model_dump(mode="json"),
            completed_at=now(),
        ),
        run.revision,
    )
    assert conversations.memory(conversation.conversation_id).last_run_id is None
    for _ in range(2):
        RunOrchestrator(
            SQLiteConversationRepository(str(tmp_path / "runtime.db")),
            SQLiteRunRepository(str(tmp_path / "runtime.db")),
            ask,
            ConversationContextResolver(FakeModel()),
            InlineRunScheduler(),
        )
    assert conversations.memory(conversation.conversation_id).last_run_id == run.run_id
    assert len(conversations.messages(conversation.conversation_id)) == 2
    assert [item.event_type for item in runs.events(run.run_id)] == ["RUN_COMPLETED"]


def test_restart_repairs_waiting_clarification_once(tmp_path):
    scheduler = QueuedScheduler()
    conversations, runs, ask, runtime = build(tmp_path, scheduler=scheduler)
    conversation = ConversationService(conversations).create()
    run = runtime.create(conversation.conversation_id, "销售额是多少？")
    response = AskResponse(
        question="销售额是多少？",
        status=AskStatus.CLARIFICATION_REQUIRED,
        clarification=[AskClarification(question="哪个口径？")],
    )
    run = runs.save(transition(run, RunStatus.CONTEXTUALIZING), run.revision)
    run = runs.save(transition(run, RunStatus.DISCOVERING), run.revision)
    runs.save(
        transition(run, RunStatus.WAITING_USER, response_json=response.model_dump(mode="json")),
        run.revision,
    )
    runs.append_event(run.run_id, "RUN_STARTED", {})
    for _ in range(2):
        RunOrchestrator(
            SQLiteConversationRepository(str(tmp_path / "runtime.db")),
            SQLiteRunRepository(str(tmp_path / "runtime.db")),
            ask,
            ConversationContextResolver(FakeModel()),
            InlineRunScheduler(),
        )
    assert len(conversations.messages(conversation.conversation_id)) == 2
    assert runs.get(run.run_id).status == RunStatus.WAITING_USER
    assert runs.last_event(run.run_id).event_type == "CLARIFICATION_REQUIRED"
