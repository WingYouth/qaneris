"""Conversation through the real Ask grounding, compiler and SQLite adapter."""

from test_grounded_sqlite_pipeline_execution import (
    governed_assets,
    registry_with,
    scanned,
)

from smartdata.application.service import SmartDataService
from smartdata.capabilities.ask import ServiceAskCapability
from smartdata.catalog import Catalog
from smartdata.contracts.semantic import BusinessQuery
from smartdata.conversation.context import ConversationContextResolver
from smartdata.conversation.repository import SQLiteConversationRepository
from smartdata.conversation.service import ConversationService
from smartdata.runtime.models import RunStatus
from smartdata.runtime.orchestrator import RunOrchestrator
from smartdata.runtime.repository import SQLiteRunRepository
from smartdata.runtime.scheduler import InlineRunScheduler


class Model:
    def parse_business_query(self, question, rule_facts):
        return BusinessQuery(
            question=question,
            objective="lookup",
            metrics=["销售额"],
            dimensions=["地区"] if "地区" in question else [],
        )

    def resolve_followup(self, question, context):
        return "按地区看销售额"

    def answer_question(self, *args, **kwargs):
        raise RuntimeError("deterministic answer fallback")


def test_local_conversation_uses_real_sqlite_twice_and_reopens(tmp_path):
    _driver, reader, catalog_path, _version, _database = scanned(tmp_path)
    with Catalog(catalog_path)._connect() as db:
        db.execute("UPDATE datasource SET status='ready'")
    registry_with(governed_assets(reader)[:2], catalog_path)
    model = Model()
    service = SmartDataService(Catalog(catalog_path), model=model, graph_reader=reader)
    conversations = SQLiteConversationRepository(str(catalog_path))
    runs = SQLiteRunRepository(str(catalog_path))
    runtime = RunOrchestrator(
        conversations,
        runs,
        ServiceAskCapability(service),
        ConversationContextResolver(model),
        InlineRunScheduler(),
    )
    conversation = ConversationService(conversations).create(datasource_ids=["ds_sales"])
    first = runtime.create(conversation.conversation_id, "销售额是多少？")
    second = runtime.create(conversation.conversation_id, "那按地区看呢？")
    assert runs.get(first.run_id).status == RunStatus.COMPLETED
    assert runs.get(second.run_id).status == RunStatus.COMPLETED
    assert runs.get(first.run_id).response_json["result"]["row_count"] == 1
    assert runs.get(second.run_id).response_json["result"]["row_count"] == 1
    assert runs.get(second.run_id).resolved_question == "按地区看销售额"
    reopened = SQLiteConversationRepository(str(catalog_path))
    assert len(reopened.messages(conversation.conversation_id)) == 4
    memory = reopened.memory(conversation.conversation_id)
    assert memory.active_dimensions == ["地区"]
    assert memory.last_result_shape["row_count"] == 1
    assert "rows" not in memory.model_dump_json()
    assert memory.evidence_refs == [f"evidence:{second.run_id}"]
