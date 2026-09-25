"""Diagnostic loop invariants at the Ask capability boundary."""

import sqlite3
from collections import Counter
from dataclasses import dataclass

import pytest

from qaneris.contracts import (
    AskClarification,
    AskEvent,
    AskResponse,
    AskStatus,
    ErrorDetail,
)
from qaneris.contracts.query import ExecutionEvidence, GroundedQueryResult
from qaneris.conversation.context import ConversationContextResolver
from qaneris.conversation.repository import SQLiteConversationRepository
from qaneris.conversation.service import ConversationService
from qaneris.diagnostics.models import DiagnosticDecision, EvidenceQuestion, TaskStatus
from qaneris.diagnostics.policy import select_questions
from qaneris.runtime.models import RunStatus
from qaneris.runtime.orchestrator import RunOrchestrator
from qaneris.runtime.repository import SQLiteRunRepository
from qaneris.runtime.scheduler import InlineRunScheduler


@dataclass
class Record:
    event: AskEvent
    response: AskResponse | None = None
    exception: Exception | None = None


def completed(question, source="source-a", amount=12):
    return AskResponse(
        question=question,
        status=AskStatus.COMPLETED,
        answer=f"销售额下降 {amount}%",
        result=GroundedQueryResult(
            plan_id="plan",
            datasource_id=source,
            query_language="sql",
            columns=["销售额", "password"],
            rows=[{"销售额": amount, "password": "hidden"}],
            row_count=1,
            scan_version=7,
        ),
        evidence=ExecutionEvidence(
            plan_id="plan",
            datasource_id=source,
            scan_version=7,
            query_language="sql",
            display_command="SELECT <redacted>",
            row_count=1,
        ),
    )


class FakeAsk:
    def __init__(self, replies=None):
        self.requests = []
        self.replies = replies or {}
        self.counts = Counter()

    def execute(self, request):
        self.requests.append(request)
        self.counts[request.question] += 1
        reply = self.replies.get(request.question)
        response = reply(self.counts[request.question]) if callable(reply) else reply
        response = response or completed(request.question)
        yield Record(AskEvent(event_type="accepted", sequence=1, correlation_id="test"))
        yield Record(
            AskEvent(event_type="result_ready", sequence=2, correlation_id="test"), response
        )


class FakeModel:
    def __init__(self, rounds=None, answer="数据显示销售额下降 12%，当前数据不足以证明因果关系。"):
        self.rounds = rounds or [["问题一", "问题二", "问题三"], ["问题四", "问题五", "问题六"]]
        self.answer = answer
        self.contexts = []

    def propose_evidence_questions(self, context):
        self.contexts.append(context)
        index = len(self.contexts) - 1
        questions = self.rounds[index] if index < len(self.rounds) else ["额外问题"]
        return DiagnosticDecision(
            action="query",
            questions=[
                EvidenceQuestion(question=question, purpose=f"验证{question}", priority=i + 1)
                for i, question in enumerate(questions)
            ],
        )

    def synthesize_diagnosis(self, context):
        self.synthesis = context
        return self.answer


def build(tmp_path, ask=None, model=None, scope=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = str(tmp_path / "runtime.db")
    conversations = SQLiteConversationRepository(path)
    runs = SQLiteRunRepository(path)
    model = model or FakeModel()
    ask = ask or FakeAsk()
    runtime = RunOrchestrator(
        conversations,
        runs,
        ask,
        ConversationContextResolver(None),
        InlineRunScheduler(),
        diagnostic_model=model,
    )
    conversation = ConversationService(conversations).create(datasource_ids=scope or [])
    return conversations, runs, ask, model, runtime, conversation


def test_budget_rounds_pinning_and_reopen(tmp_path):
    conversations, runs, ask, model, runtime, conversation = build(tmp_path)
    run = runtime.create(conversation.conversation_id, "为什么销售额同比下降？")
    assert runs.get(run.run_id).status == RunStatus.COMPLETED
    assert runs.get(run.run_id).run_kind == "diagnostic"
    assert len(ask.requests) == 5
    assert ask.requests[0].datasource_id is None
    assert all(item.datasource_id == "source-a" for item in ask.requests[1:])
    assert len(model.contexts) == 2
    assert runtime.diagnostics.repository.checkpoint(run.run_id).remaining_budget == 0
    assert all(
        item.status == TaskStatus.COMPLETED
        for item in runtime.diagnostics.repository.tasks(run.run_id)
    )
    assert "hidden" not in model.contexts[1].model_dump_json()
    assert "hidden" not in model.synthesis.model_dump_json()
    assert len(conversations.memory(conversation.conversation_id).diagnostic_findings) == 1
    assert (
        SQLiteConversationRepository(str(tmp_path / "runtime.db"))
        .memory(conversation.conversation_id)
        .diagnostic_findings[0]["evidence_refs"]
    )
    assert runs.events(run.run_id, 2)[0].sequence == 3
    assert len(ask.requests) == 5


def test_retry_only_failed_task(tmp_path):
    replies = {
        "问题三": lambda count: (
            AskResponse(
                question="问题三",
                status=AskStatus.FAILED,
                error=ErrorDetail(code="timeout", message="timeout"),
            )
            if count == 1
            else completed("问题三")
        )
    }
    _, runs, ask, _, runtime, conversation = build(tmp_path, FakeAsk(replies))
    run = runtime.create(conversation.conversation_id, "为什么销售额同比下降？")
    assert runs.get(run.run_id).status == RunStatus.FAILED
    assert runs.get(run.run_id).retryable
    runtime.retry(run.run_id)
    assert runs.get(run.run_id).status == RunStatus.COMPLETED
    assert ask.counts["问题一"] == ask.counts["问题二"] == 1
    assert ask.counts["问题三"] == 2


def test_restart_retry_keeps_completed_evidence(tmp_path):
    replies = {
        "问题三": lambda count: (
            AskResponse(
                question="问题三",
                status=AskStatus.FAILED,
                error=ErrorDetail(code="timeout", message="timeout"),
            )
            if count == 1
            else completed("问题三")
        ),
    }
    conversations, runs, ask, model, runtime, conversation = build(tmp_path, FakeAsk(replies))
    run = runtime.create(conversation.conversation_id, "为什么销售额同比下降？")
    assert runs.get(run.run_id).status == RunStatus.FAILED
    reopened = RunOrchestrator(
        SQLiteConversationRepository(str(tmp_path / "runtime.db")),
        SQLiteRunRepository(str(tmp_path / "runtime.db")),
        ask,
        ConversationContextResolver(None),
        InlineRunScheduler(),
        diagnostic_model=model,
    )
    reopened.retry(run.run_id)
    assert runs.get(run.run_id).status == RunStatus.COMPLETED
    assert ask.counts["问题一"] == ask.counts["问题二"] == 1
    assert ask.counts["问题三"] == 2
    assert len(conversations.memory(conversation.conversation_id).diagnostic_findings) == 1


def test_duplicate_second_round_is_skipped(tmp_path):
    model = FakeModel(rounds=[["问题一"], [" 问题一 ", "问题二"]])
    _, runs, ask, _, runtime, conversation = build(tmp_path, model=model)
    run = runtime.create(conversation.conversation_id, "为什么销售额同比下降？")
    assert runs.get(run.run_id).status == RunStatus.COMPLETED
    assert len(ask.requests) == 2
    assert any(
        task.status == TaskStatus.SKIPPED
        for task in runtime.diagnostics.repository.tasks(run.run_id)
    )


def test_synthesis_outage_uses_verified_fallback(tmp_path):
    class Outage(FakeModel):
        def synthesize_diagnosis(self, context):
            raise RuntimeError("429")

    _, runs, _, _, runtime, conversation = build(tmp_path, model=Outage())
    run = runtime.create(conversation.conversation_id, "为什么销售额同比下降？")
    assert runs.get(run.run_id).status == RunStatus.COMPLETED
    assert runs.get(run.run_id).response_json["analysis"]["answer_source"] == (
        "diagnostic_deterministic_fallback"
    )


def test_cancel_preserves_completed_task_without_memory(tmp_path):
    class CancelAsk(FakeAsk):
        def execute(self, request):
            self.requests.append(request)
            self.counts[request.question] += 1
            yield Record(AskEvent(event_type="accepted", sequence=1, correlation_id="test"))
            if len(self.requests) == 2:
                with sqlite3.connect(runtime.runs.path) as db:
                    run_id = db.execute("SELECT id FROM run LIMIT 1").fetchone()[0]
                runtime.cancel(run_id)
            yield Record(
                AskEvent(event_type="result_ready", sequence=2, correlation_id="test"),
                completed(request.question),
            )

    conversations, runs, _, _, runtime, conversation = build(tmp_path, CancelAsk())
    run = runtime.create(conversation.conversation_id, "为什么销售额同比下降？")
    assert runs.get(run.run_id).status == RunStatus.CANCELLED
    statuses = [task.status for task in runtime.diagnostics.repository.tasks(run.run_id)]
    assert statuses[0] == TaskStatus.COMPLETED
    assert statuses[1] == TaskStatus.SKIPPED
    assert not conversations.memory(conversation.conversation_id).diagnostic_findings


def test_single_source_mismatch_is_blocked(tmp_path):
    class MismatchAsk(FakeAsk):
        def execute(self, request):
            self.requests.append(request)
            source = "source-a" if len(self.requests) == 1 else "source-b"
            yield Record(
                AskEvent(event_type="result_ready", sequence=1, correlation_id="test"),
                completed(request.question, source),
            )

    conversations, runs, ask, _, runtime, conversation = build(tmp_path, MismatchAsk())
    run = runtime.create(conversation.conversation_id, "为什么销售额同比下降？")
    assert ask.requests[1].datasource_id == "source-a"
    assert runs.get(run.run_id).status == RunStatus.BLOCKED
    assert runs.get(run.run_id).failure_code == "diagnostic_datasource_mismatch"
    assert not conversations.memory(conversation.conversation_id).diagnostic_findings


def test_diagnostic_followup_and_new_topic_routing(tmp_path):
    class Followup(FakeModel):
        def resolve_followup(self, question, context):
            return "销售额按产品看"

    conversations, runs, ask, model, runtime, conversation = build(tmp_path, model=Followup())
    first = runtime.create(conversation.conversation_id, "为什么销售额同比下降？")
    assert runs.get(first.run_id).status == RunStatus.COMPLETED
    memory = conversations.memory(conversation.conversation_id)
    conversations.put_memory(
        conversation.conversation_id,
        memory.model_copy(
            update={
                "confirmed_business_query": {"question": "销售额"},
                "active_metrics": ["销售额"],
            }
        ),
        conversations.memory_revision(conversation.conversation_id),
    )
    runtime.resolver = ConversationContextResolver(model)
    followup = runtime.create(conversation.conversation_id, "那产品 A 再深入看看")
    assert runs.get(followup.run_id).run_kind == "diagnostic"
    assert runs.get(followup.run_id).status == RunStatus.COMPLETED
    standalone = runtime.create(conversation.conversation_id, "最近30天订单量是多少？")
    assert runs.get(standalone.run_id).run_kind == "normal"
    assert ask.requests[-1].question == "最近30天订单量是多少？"


def test_clarification_same_run_and_task(tmp_path):
    replies = {
        "问题一": AskResponse(
            question="问题一",
            status=AskStatus.CLARIFICATION_REQUIRED,
            answer="请选择销售额口径",
            clarification=[
                AskClarification(question="请选择销售额口径", options=["实收销售额", "含税销售额"])
            ],
        ),
        "问题一。用户澄清：实收销售额": completed("问题一。用户澄清：实收销售额"),
    }
    conversations, runs, ask, _, runtime, conversation = build(tmp_path, FakeAsk(replies))
    run = runtime.create(conversation.conversation_id, "为什么销售额同比下降？")
    assert runs.get(run.run_id).status == RunStatus.WAITING_USER
    task_id = runtime.diagnostics.repository.tasks(run.run_id)[0].id
    assert not conversations.memory(conversation.conversation_id).diagnostic_findings
    runtime.clarify(run.run_id, "实收销售额")
    assert runs.get(run.run_id).status == RunStatus.COMPLETED
    assert runtime.diagnostics.repository.tasks(run.run_id)[0].id == task_id
    assert ask.counts["问题一"] == 1
    assert ask.counts["问题一。用户澄清：实收销售额"] == 1


def test_first_turn_baseline_clarification_resumes_same_run(tmp_path):
    class Baseline(FakeModel):
        def propose_evidence_questions(self, context):
            if "用户澄清" not in context.question:
                return DiagnosticDecision(
                    action="clarify", clarification="请明确与哪个时间段比较？"
                )
            return super().propose_evidence_questions(context)

    conversations, runs, ask, _, runtime, conversation = build(tmp_path, model=Baseline())
    run = runtime.create(conversation.conversation_id, "为什么八月销售额下降？")
    assert runs.get(run.run_id).status == RunStatus.WAITING_USER
    assert not ask.requests
    runtime.clarify(run.run_id, "与去年八月比较")
    assert runs.get(run.run_id).status == RunStatus.COMPLETED
    assert len(ask.requests) == 5
    assert len(conversations.memory(conversation.conversation_id).diagnostic_findings) == 1


def test_missing_data_and_number_guard(tmp_path):
    replies = {
        "问题一": AskResponse(
            question="问题一",
            status=AskStatus.CLARIFICATION_REQUIRED,
            answer="当前数据没有受治理渠道维度",
            clarification=[AskClarification(question="当前数据没有受治理渠道维度")],
        )
    }
    model = FakeModel(answer="销售额下降 37%，广告减少导致销售额下降。")
    _, runs, _, model, runtime, conversation = build(tmp_path, FakeAsk(replies), model)
    run = runtime.create(conversation.conversation_id, "为什么销售额同比下降？")
    assert runs.get(run.run_id).status == RunStatus.COMPLETED
    response = AskResponse.model_validate(runs.get(run.run_id).response_json)
    assert response.analysis["answer_source"] == "diagnostic_deterministic_fallback"
    assert "37%" not in response.answer
    assert "未验证" in response.answer
    assert len(model.synthesis.unavailable) == 1


def test_planner_outage_without_and_with_evidence(tmp_path):
    class Outage(FakeModel):
        def propose_evidence_questions(self, context):
            if context.round == 2 or fail_first:
                raise RuntimeError("429")
            return super().propose_evidence_questions(context)

    fail_first = True
    _, runs, ask, _, runtime, conversation = build(tmp_path, model=Outage())
    run = runtime.create(conversation.conversation_id, "为什么销售额同比下降？")
    assert runs.get(run.run_id).status == RunStatus.BLOCKED
    assert runs.get(run.run_id).failure_code == "diagnostic_model_unavailable"
    assert not ask.requests
    fail_first = False
    _, runs, _, _, runtime, conversation = build(tmp_path / "second", model=Outage())
    run = runtime.create(conversation.conversation_id, "为什么销售额同比下降？")
    assert runs.get(run.run_id).status == RunStatus.COMPLETED
    assert runs.get(run.run_id).response_json["analysis"]["answer_source"] == (
        "diagnostic_deterministic_fallback"
    )


def test_policy_rejects_native_and_duplicates():
    decision = DiagnosticDecision(
        action="query",
        questions=[
            EvidenceQuestion(question="按地区看销售额", purpose="定位地区"),
            EvidenceQuestion(question=" 按地区看销售额 ", purpose="定位地区"),
        ],
    )
    selected, duplicates = select_questions(decision, set(), 5, runtime_budget())
    assert len(selected) == len(duplicates) == 1
    with pytest.raises(ValueError):
        select_questions(
            DiagnosticDecision(
                action="query",
                questions=[EvidenceQuestion(question="SELECT * FROM orders", purpose="查询")],
            ),
            set(),
            5,
            runtime_budget(),
        )
    with pytest.raises(ValueError):
        DiagnosticDecision(action="query")


def runtime_budget():
    from qaneris.diagnostics.models import DiagnosticBudget

    return DiagnosticBudget()
