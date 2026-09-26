from types import SimpleNamespace

import pytest

from qaneris.contracts.semantic import BusinessQuery
from qaneris.contracts import AskEvent, AskResponse, AskStatus, NormalizedResult
from qaneris.conversation.context import ConversationContextResolver
from qaneris.conversation.repository import SQLiteConversationRepository
from qaneris.conversation.service import ConversationService
from qaneris.federation.governance import FederationFailure
from qaneris.federation.router import FederationRouter
from qaneris.runtime.models import RunStatus
from qaneris.runtime.orchestrator import RunOrchestrator
from qaneris.runtime.repository import SQLiteRunRepository
from qaneris.runtime.scheduler import InlineRunScheduler


class Service:
    def list_datasources(self, workspace_id):
        return [
            SimpleNamespace(id="mysql-id", name="MySQL", status="ready", kind="relational", driver="mysql"),
            SimpleNamespace(id="pg-id", name="PostgreSQL", status="ready", kind="relational", driver="postgresql"),
        ]

    def understand_intent(self, question):
        return SimpleNamespace(business_query=BusinessQuery(
            question=question, objective="lookup", metrics=["销售额"],
        ))

    def retrieve_semantics(self, query, workspace_id, datasource_id, limit):
        return SimpleNamespace(candidates=[])


def test_explicit_source_routes_without_published_business_assets():
    decision = FederationRouter(Service()).route(
        "MySQL 的销售额是多少？", "default", ["mysql-id", "pg-id"]
    )
    assert decision.kind == "single_source"
    assert decision.single_datasource_id == "mysql-id"


def test_ambiguous_source_does_not_pick_arbitrarily():
    with pytest.raises(FederationFailure, match="source_selection_ambiguous"):
        FederationRouter(Service()).route("今年的销售额是多少？", "default", ["mysql-id", "pg-id"])


def test_explicit_comparison_without_business_assets_reports_missing_semantics():
    with pytest.raises(FederationFailure, match="federation_semantics_missing"):
        FederationRouter(Service()).route(
            "比较 MySQL 和 PostgreSQL 的销售额", "default", ["mysql-id", "pg-id"]
        )


def test_ambiguous_question_asks_for_source_and_resumes_same_run(tmp_path):
    class Ask:
        def execute(self, request):
            assert request.datasource_id == "mysql-id"
            yield SimpleNamespace(
                event=AskEvent(event_type="done", sequence=1, correlation_id="test"),
                response=AskResponse(
                    question=request.question, status=AskStatus.COMPLETED, answer="100",
                    result=NormalizedResult(
                        source="mysql-id", dataset="orders", columns=["amount"],
                        rows=[{"amount": 100}], row_count=1,
                    ),
                ),
                exception=None,
            )

    path = str(tmp_path / "runtime.db")
    conversations = SQLiteConversationRepository(path)
    runs = SQLiteRunRepository(path)
    runtime = RunOrchestrator(
        conversations, runs, Ask(), ConversationContextResolver(None),
        InlineRunScheduler(), federation_service=Service(),
    )
    runtime.federation_router = FederationRouter(Service())
    conversation = ConversationService(conversations).create()
    run = runtime.create(conversation.conversation_id, "今年的销售额是多少？")
    waiting = runs.get(run.run_id)
    assert waiting.status is RunStatus.WAITING_USER
    assert waiting.response_json["clarification"][0]["options"] == ["MySQL", "PostgreSQL"]

    runtime.clarify(run.run_id, "MySQL")
    completed = runs.get(run.run_id)
    assert completed.status is RunStatus.COMPLETED
    assert completed.response_json["result"]["rows"] == [{"amount": 100}]
