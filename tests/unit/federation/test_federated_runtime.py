"""Durable source attempts and replay at the conversation boundary."""

from collections import Counter
from dataclasses import dataclass

from smartdata.contracts import (
    AskClarification,
    AskEvent,
    AskResponse,
    AskStatus,
    ExecutionEvidence,
    NormalizedResult,
)
from smartdata.contracts.semantic import BusinessQuery
from smartdata.conversation.context import ConversationContextResolver
from smartdata.conversation.repository import SQLiteConversationRepository
from smartdata.conversation.service import ConversationService
from smartdata.federation.models import FederatedPlanDraft
from smartdata.federation.router import RoutingDecision
from smartdata.runtime.models import RunStatus
from smartdata.runtime.orchestrator import RunOrchestrator
from smartdata.runtime.repository import SQLiteRunRepository
from smartdata.runtime.scheduler import InlineRunScheduler


@dataclass
class Source:
    id: str
    name: str
    status: str = "ready"


class Service:
    def list_datasources(self, workspace_id):
        return [Source("a", "a"), Source("b", "b")]


class Router:
    def route(self, question, workspace_id, allowed):
        query = BusinessQuery(question=question, objective="comparison",
                              comparison={"comparison_type": "group"},
                              metrics=["销售额", "回款"])
        return RoutingDecision("federated", query.model_dump(mode="json"), [
            {"datasource_id": source, "business_name": source, "assets": []}
            for source in allowed
        ])


class Model:
    def plan_federation(self, context):
        return FederatedPlanDraft.model_validate({
            "source_tasks": [
                {"datasource_id": "a", "question": "销售额是多少？", "purpose": "销售额",
                 "expected_shape": "scalar"},
                {"datasource_id": "b", "question": "回款是多少？", "purpose": "回款",
                 "expected_shape": "scalar"},
            ],
            "merge": {"operation": "compare_scalars", "scalar_operation": "difference"},
        })

    def answer_question(self, question, result):
        return "差额 30"  # The shared number guard must reject this invention.


@dataclass
class Record:
    event: AskEvent
    response: AskResponse | None = None
    exception: Exception | None = None


class Transient(Exception):
    code = "transient_network"


class Ask:
    def __init__(self, mode):
        self.mode = mode
        self.counts = Counter()

    def execute(self, request):
        source = request.datasource_id
        self.counts[source] += 1
        if source == "b" and self.counts[source] == 1:
            if self.mode == "failure":
                yield Record(AskEvent(event_type="error", sequence=1,
                                      correlation_id="fake"), exception=Transient())
                return
            if self.mode == "clarification":
                yield Record(AskEvent(event_type="done", sequence=1,
                                      correlation_id="fake"), response=AskResponse(
                    question=request.question, status=AskStatus.CLARIFICATION_REQUIRED,
                    answer="请选择回款口径", clarification=[AskClarification(question="请选择回款口径")],
                ))
                return
        amount = 100 if source == "a" else 80
        yield Record(AskEvent(event_type="done", sequence=1, correlation_id="fake"),
                     response=AskResponse(
                         question=request.question, status=AskStatus.COMPLETED,
                         answer=str(amount),
                         result=NormalizedResult(source=source, dataset="test",
                                                 columns=["amount"], rows=[{"amount": amount}],
                                                 row_count=1),
                         evidence=ExecutionEvidence(
                             plan_id="p", datasource_id=source, scan_version=1,
                             query_language="sql", display_command="SELECT <redacted>",
                             row_count=1,
                         ),
                     ))


class DeferredScheduler:
    def __init__(self):
        self.work = None

    def submit(self, run_id, work):
        self.work = (run_id, work)

    def flush(self):
        run_id, work = self.work
        work(run_id)


def build(tmp_path, mode):
    path = str(tmp_path / "runtime.db")
    conversations = SQLiteConversationRepository(path)
    runs = SQLiteRunRepository(path)
    ask = Ask(mode)
    runtime = RunOrchestrator(
        conversations, runs, ask, ConversationContextResolver(None),
        InlineRunScheduler(), federation_service=Service(), federation_model=Model(),
    )
    runtime.federation_router = Router()
    conversation = ConversationService(conversations).create(datasource_ids=["a", "b"])
    return runtime, conversations, runs, ask, conversation


def test_retry_reuses_completed_source_and_sse_replays(tmp_path):
    runtime, conversations, runs, ask, conversation = build(tmp_path, "failure")
    run = runtime.create(conversation.conversation_id, "比较销售额和回款")
    failed = runs.get(run.run_id)
    assert failed.status == RunStatus.FAILED and failed.retryable
    assert ask.counts == {"a": 1, "b": 1}
    runtime.retry(run.run_id)
    completed = runs.get(run.run_id)
    assert completed.status == RunStatus.COMPLETED
    assert ask.counts == {"a": 1, "b": 2}
    assert completed.response_json["merged_result"]["rows"][0]["difference"] == 20
    assert completed.response_json["answer_source"] == "federated_deterministic_fallback"
    events = runs.events(run.run_id)
    assert [event.sequence for event in runs.events(run.run_id, events[2].sequence)] == [
        event.sequence for event in events[3:]
    ]
    assert ask.counts == {"a": 1, "b": 2}
    assert conversations.memory(conversation.conversation_id).active_metrics == ["销售额", "回款"]


def test_clarification_resumes_same_source_task(tmp_path):
    runtime, _conversations, runs, ask, conversation = build(tmp_path, "clarification")
    run = runtime.create(conversation.conversation_id, "比较销售额和回款")
    assert runs.get(run.run_id).status == RunStatus.WAITING_USER
    assert [task.status for task in runtime.federation.repository.tasks(run.run_id)] == [
        "COMPLETED", "WAITING_USER"
    ]
    runtime.clarify(run.run_id, "已到账金额")
    assert runs.get(run.run_id).status == RunStatus.COMPLETED
    assert ask.counts == {"a": 1, "b": 2}
    assert [task.attempt for task in runtime.federation.repository.tasks(run.run_id)] == [1, 2]


def test_restart_reuses_completed_source(tmp_path):
    runtime, conversations, runs, ask, conversation = build(tmp_path, "failure")
    run = runtime.create(conversation.conversation_id, "比较销售额和回款")
    failed = runs.get(run.run_id)
    assert failed.status == RunStatus.FAILED
    runs.save(failed.model_copy(update={"current_stage": "retry_queued"}), failed.revision)
    restarted = RunOrchestrator(
        conversations, runs, ask, ConversationContextResolver(None),
        InlineRunScheduler(), federation_service=Service(), federation_model=Model(),
    )
    restarted.federation_router = Router()
    assert runs.get(run.run_id).status == RunStatus.COMPLETED
    assert ask.counts == {"a": 1, "b": 2}


def test_cancel_stops_before_next_source_and_merge(tmp_path):
    path = str(tmp_path / "runtime.db")
    conversations = SQLiteConversationRepository(path)
    runs = SQLiteRunRepository(path)
    scheduler = DeferredScheduler()
    ask = Ask("none")
    runtime = RunOrchestrator(
        conversations, runs, ask, ConversationContextResolver(None), scheduler,
        federation_service=Service(), federation_model=Model(),
    )
    runtime.federation_router = Router()
    conversation = ConversationService(conversations).create(datasource_ids=["a", "b"])
    run = runtime.create(conversation.conversation_id, "比较销售额和回款")
    original_execute = ask.execute

    def cancel_during_a(request):
        if request.datasource_id == "a":
            runtime.cancel(run.run_id)
        yield from original_execute(request)

    ask.execute = cancel_during_a
    scheduler.flush()
    assert runs.get(run.run_id).status == RunStatus.CANCELLED
    assert ask.counts["b"] == 0
    assert conversations.memory(conversation.conversation_id).last_run_id is None
    assert runtime.federation.repository.tasks(run.run_id)[0].status == "SKIPPED"
