"""Diagnostic questions traverse real grounding, planning and SQLite execution."""

import sqlite3

from test_grounded_sqlite_pipeline_execution import governed_assets, registry_with, scanned

from qaneris.application.service import QanerisService
from qaneris.capabilities.ask import ServiceAskCapability
from qaneris.catalog import Catalog
from qaneris.contracts.semantic import BusinessQuery
from qaneris.conversation.context import ConversationContextResolver
from qaneris.conversation.repository import SQLiteConversationRepository
from qaneris.conversation.service import ConversationService
from qaneris.diagnostics.models import DiagnosticDecision, EvidenceQuestion
from qaneris.runtime.models import RunStatus
from qaneris.runtime.orchestrator import RunOrchestrator
from qaneris.runtime.repository import SQLiteRunRepository
from qaneris.runtime.scheduler import InlineRunScheduler


class Model:
    def parse_business_query(self, question, rule_facts):
        return BusinessQuery(
            question=question,
            objective="lookup",
            metrics=["销售额"],
            dimensions=["地区"],
            time_expression=rule_facts.get("time_expression"),
        )

    def resolve_followup(self, question, context):
        return question

    def answer_question(self, *args, **kwargs):
        raise RuntimeError("use deterministic Ask answer")

    def propose_evidence_questions(self, context):
        dates = ["2025-08-01 至 2025-08-31", "2026-08-01 至 2026-08-31"]
        if context.round == 1:
            return DiagnosticDecision(
                action="query",
                questions=[
                    EvidenceQuestion(
                        question=f"{date}销售额按地区", purpose=f"验证{date}地区销售额"
                    )
                    for date in dates
                ],
            )
        return DiagnosticDecision(action="stop")

    def synthesize_diagnosis(self, context):
        assert len(context.observations) == 2
        return "数据库结果显示华东销售额在两个期间有变化；当前证据不足以证明因果关系。"


def test_real_sqlite_diagnostic_reopens_with_evidence(tmp_path):
    _, reader, catalog_path, _, database = scanned(tmp_path)
    with sqlite3.connect(database) as db:
        db.execute("DELETE FROM orders")
        db.executemany(
            "INSERT INTO orders VALUES (?, ?, ?, ?, ?)",
            [
                (1, 1, 1000.0, "华东", "2025-08-12"),
                (2, 2, 800.0, "华南", "2025-08-12"),
                (3, 1, 700.0, "华东", "2026-08-12"),
                (4, 2, 790.0, "华南", "2026-08-12"),
            ],
        )
    with Catalog(catalog_path)._connect() as db:
        db.execute("UPDATE datasource SET status='ready'")
    assets = governed_assets(reader)[:3]
    assets[2] = assets[2].model_copy(update={"time_axis": True})
    registry_with(assets, catalog_path)
    model = Model()
    service = QanerisService(Catalog(catalog_path), model=model, graph_reader=reader)
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
    prior = runtime.create(conversation.conversation_id, "销售额按地区")
    assert runs.get(prior.run_id).status == RunStatus.COMPLETED
    memory_before = conversations.memory(conversation.conversation_id)
    run = runtime.create(conversation.conversation_id, "为什么华东同比下降更多？")
    assert runs.get(run.run_id).status == RunStatus.COMPLETED
    tasks = runtime.diagnostics.repository.tasks(run.run_id)
    assert len(tasks) == 2
    assert all(task.evidence_ref for task in tasks)
    assert all(task.ask_response_json["result"]["row_count"] >= 1 for task in tasks)
    reopened = SQLiteConversationRepository(str(catalog_path))
    memory = reopened.memory(conversation.conversation_id)
    assert memory.active_metrics == memory_before.active_metrics
    assert memory.active_dimensions == memory_before.active_dimensions
    assert len(memory.diagnostic_findings) == 1
    assert len(memory.diagnostic_findings[0]["evidence_refs"]) == 2
    new_topic = runtime.create(conversation.conversation_id, "销售额按地区")
    assert runs.get(new_topic.run_id).run_kind == "normal"
    assert runs.get(new_topic.run_id).status == RunStatus.COMPLETED
    assert len(reopened.memory(conversation.conversation_id).diagnostic_findings) == 1
