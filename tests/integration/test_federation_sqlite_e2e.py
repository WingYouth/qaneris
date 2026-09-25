"""Two real SQLite sources through Conversation -> Ask -> governed merge."""

import json
import sqlite3
from datetime import UTC, datetime

from _neo4j_fake import FakeNeo4jDriver, graph_reader, graph_store

from smartdata.adapters import create_adapter
from smartdata.application.service import SmartDataService
from smartdata.capabilities.ask import ServiceAskCapability
from smartdata.catalog import Catalog
from smartdata.contracts import Datasource
from smartdata.contracts.profile import DataSourceProfile, ScanSnapshot
from smartdata.contracts.semantic import BusinessQuery
from smartdata.conversation.context import ConversationContextResolver
from smartdata.conversation.repository import SQLiteConversationRepository
from smartdata.conversation.service import ConversationService
from smartdata.federation.models import FederatedPlanDraft
from smartdata.graph.reading import GraphStructureRequest
from smartdata.runtime.models import RunStatus
from smartdata.runtime.orchestrator import RunOrchestrator
from smartdata.runtime.repository import SQLiteRunRepository
from smartdata.runtime.scheduler import InlineRunScheduler
from smartdata.scan import ScanService
from smartdata.semantic import SemanticAsset, SQLiteSemanticAssetRegistry


class Model:
    def parse_business_query(self, question, rule_facts):
        if "已付款但未发货" in question:
            return BusinessQuery(question=question, objective="lookup",
                                 entities=["已付款订单", "已发货订单"])
        if "订单销售额" in question and "回款金额" in question:
            return BusinessQuery(question=question, objective="comparison",
                                 comparison={"comparison_type": "group"},
                                 metrics=["订单销售额", "回款金额"])
        if "订单销售额" in question:
            return BusinessQuery(question=question, objective="lookup", metrics=["订单销售额"])
        if "回款金额" in question:
            return BusinessQuery(question=question, objective="lookup", metrics=["回款金额"])
        return BusinessQuery(question=question, objective="lookup", dimensions=["订单号"])

    def resolve_followup(self, question, context):
        return question

    def plan_federation(self, context):
        if "已付款但未发货" in context.question:
            return FederatedPlanDraft.model_validate({
                "source_tasks": [
                    {"datasource_id": "payments", "question": "列出已付款订单的订单号",
                     "purpose": "获取付款订单", "expected_shape": "rows"},
                    {"datasource_id": "shipments", "question": "列出已发货订单的订单号",
                     "purpose": "获取发货订单", "expected_shape": "rows"},
                ],
                "merge": {"operation": "keyed_join", "mapping_id": (
                    context.confirmed_join_mappings[0]["mapping_id"]
                    if context.confirmed_join_mappings else None
                ),
                          "join_mode": "anti"},
            })
        return FederatedPlanDraft.model_validate({
            "source_tasks": [
                {"datasource_id": "orders", "question": "订单销售额是多少？",
                 "purpose": "获取订单销售额", "expected_shape": "scalar"},
                {"datasource_id": "payments", "question": "回款金额是多少？",
                 "purpose": "获取实际回款", "expected_shape": "scalar"},
            ],
            "merge": {"operation": "compare_scalars", "scalar_operation": "difference"},
        })

    def answer_question(self, question, result):
        raise RuntimeError("verify deterministic fallback")


def _setup(tmp_path):
    driver = FakeNeo4jDriver()
    reader = graph_reader(driver)
    catalog_path = tmp_path / "catalog.db"
    catalog = Catalog(catalog_path)
    registry = SQLiteSemanticAssetRegistry(catalog_path)
    for source_id, rows, metric in (
        ("orders", [(1, 100.0), (2, 50.0)], "订单销售额"),
        ("payments", [(1, 80.0), (2, 40.0), (3, 10.0)], "回款金额"),
        ("shipments", [(1, 1.0), (3, 1.0)], None),
    ):
        path = tmp_path / f"{source_id}.db"
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE items (order_id INTEGER PRIMARY KEY, amount REAL)")
            db.executemany("INSERT INTO items VALUES (?, ?)", rows)
        source = Datasource(id=source_id, name=source_id, kind="relational",
                            workspace_id="default", driver="sqlite")
        adapter = create_adapter(source_id, "relational", {"driver": "sqlite", "path": str(path)})
        ScanService(graph_store(driver)).scan(source, adapter, version=1)
        with catalog._connect() as db:
            db.execute(
                "INSERT INTO datasource(id,workspace_id,name,kind,connection_json) VALUES (?,?,?,?,?)",
                (source_id, "default", source_id, "relational",
                 json.dumps({"driver": "sqlite", "path": str(path)})),
            )
        timestamp = datetime.now(UTC)
        catalog.save_snapshot(ScanSnapshot(
            id=f"scan-{source_id}", datasource_id=source_id, version=1, status="ready",
            profile=DataSourceProfile(datasource_id=source_id, name=source_id,
                                      kind="relational", driver="sqlite"),
            started_at=timestamp, completed_at=timestamp, active=True,
        ))
        structure = reader.read_structure(GraphStructureRequest(
            workspace_id="default", datasource_id=source_id,
        ))
        obj = next(item for item in structure.data_objects if item.name == "items")
        for field_name, kind, name in (
            ("order_id", "dimension", "订单号"),
            ("amount", "metric", metric),
        ):
            if name is None:
                continue
            field = next(item for item in structure.fields
                         if item.object_id == obj.node_id and item.path == field_name)
            registry.save(SemanticAsset(
                asset_id=f"{source_id}-{field_name}", workspace_id="default",
                kind=kind, name=name, source="administrator",
                source_ref=f"seed://{source_id}/{field_name}", status="published",
                confidence=1.0, datasource_id=source_id, datasource_name=source_id,
                graph_data_object_id=obj.node_id, graph_field_id=field.node_id,
                data_object_name="items", field_path=field_name,
                default_aggregation="sum" if kind == "metric" else None,
                updated_at=timestamp,
            ))
        if source_id in {"payments", "shipments"}:
            field = next(item for item in structure.fields
                         if item.object_id == obj.node_id and item.path == "order_id")
            registry.save(SemanticAsset(
                asset_id=f"{source_id}-entity", workspace_id="default",
                kind="business_term", name="已付款订单" if source_id == "payments" else "已发货订单",
                source="administrator", source_ref=f"seed://{source_id}/entity",
                status="published", confidence=1.0, datasource_id=source_id,
                datasource_name=source_id, graph_data_object_id=obj.node_id,
                graph_field_id=field.node_id, data_object_name="items",
                field_path="order_id", updated_at=timestamp,
            ))
    model = Model()
    service = SmartDataService(catalog, model=model, graph_reader=reader)
    conversations = SQLiteConversationRepository(str(catalog_path))
    runs = SQLiteRunRepository(str(catalog_path))
    runtime = RunOrchestrator(
        conversations, runs, ServiceAskCapability(service),
        ConversationContextResolver(model), InlineRunScheduler(),
        federation_service=service,
    )
    return runtime, conversations, runs, reader


def test_two_sqlite_scalar_federation_and_memory(tmp_path):
    runtime, conversations, runs, _reader = _setup(tmp_path)
    conversation = ConversationService(conversations).create(datasource_ids=["orders", "payments"])
    run = runtime.create(conversation.conversation_id, "对比订单销售额和回款金额")
    completed = runs.get(run.run_id)
    assert completed.status == RunStatus.COMPLETED, (completed.failure_code, completed.failure_message)
    assert completed.run_kind == "federated"
    row = completed.response_json["merged_result"]["rows"][0]
    assert set(row.values()) == {150, 130, 20}
    assert completed.response_json["answer_source"] == "federated_deterministic_fallback"
    evidence = completed.response_json["federated_evidence"]
    assert len(evidence["source_evidence"]) == 2
    assert all(item["source_started_at"] and item["source_completed_at"]
               for item in evidence["source_evidence"])
    assert conversations.memory(conversation.conversation_id).active_metrics == ["订单销售额", "回款金额"]
    assert len(runtime.federation.repository.tasks(run.run_id)) == 2


def test_single_source_coverage_and_auto_scope(tmp_path):
    runtime, conversations, runs, _reader = _setup(tmp_path)
    selected = ConversationService(conversations).create(datasource_ids=["orders", "payments"])
    single = runtime.create(selected.conversation_id, "订单销售额是多少？")
    assert runs.get(single.run_id).status == RunStatus.COMPLETED
    assert runs.get(single.run_id).run_kind == "normal"
    assert runs.get(single.run_id).response_json["evidence"]["datasource_id"] == "orders"
    automatic = ConversationService(conversations).create(datasource_ids=[])
    federated = runtime.create(automatic.conversation_id, "对比订单销售额和回款金额")
    assert runs.get(federated.run_id).status == RunStatus.COMPLETED
    assert runs.get(federated.run_id).run_kind == "federated"


def test_two_sqlite_confirmed_anti_join(tmp_path):
    runtime, conversations, runs, reader = _setup(tmp_path)
    structure = reader.read_structure(GraphStructureRequest(workspace_id="default"))
    def identity(source_id):
        obj = next(item for item in structure.data_objects
                   if item.datasource_id == source_id and item.name == "items")
        return obj.node_id
    mapping = runtime.federation.governance.create(
        workspace_id="default", business_key="订单号",
        left_datasource_id="payments", left_data_object_id=identity("payments"),
        left_field_path="order_id", left_grain="one order",
        right_datasource_id="shipments", right_data_object_id=identity("shipments"),
        right_field_path="order_id", right_grain="one order",
        cardinality="ONE_TO_ONE", null_policy="REJECT",
    )
    runtime.federation.governance.confirm(mapping.mapping_id, "test-auditor")
    conversation = ConversationService(conversations).create(datasource_ids=["payments", "shipments"])
    run = runtime.create(conversation.conversation_id, "找出已付款但未发货的订单")
    completed = runs.get(run.run_id)
    assert completed.status == RunStatus.COMPLETED, (completed.failure_code, completed.failure_message)
    assert completed.response_json["merged_result"]["row_count"] == 1
    assert completed.response_json["merged_result"]["rows"][0]["left_order_id"] == 2


def test_same_named_keys_do_not_join_without_confirmation(tmp_path):
    runtime, conversations, runs, _reader = _setup(tmp_path)
    conversation = ConversationService(conversations).create(datasource_ids=["payments", "shipments"])
    run = runtime.create(conversation.conversation_id, "找出已付款但未发货的订单")
    blocked = runs.get(run.run_id)
    assert blocked.status == RunStatus.BLOCKED
    assert blocked.failure_code == "unconfirmed_mapping"
    assert runtime.federation.repository.tasks(run.run_id) == []


def test_stale_mapping_blocks_before_source_ask(tmp_path):
    runtime, conversations, runs, reader = _setup(tmp_path)
    structure = reader.read_structure(GraphStructureRequest(workspace_id="default"))
    objects = {item.datasource_id: item.node_id for item in structure.data_objects
               if item.name == "items"}
    mapping = runtime.federation.governance.create(
        workspace_id="default", business_key="订单号",
        left_datasource_id="payments", left_data_object_id=objects["payments"],
        left_field_path="order_id", left_grain="one order",
        right_datasource_id="shipments", right_data_object_id=objects["shipments"],
        right_field_path="order_id", right_grain="one order",
        cardinality="ONE_TO_ONE", null_policy="REJECT",
    )
    runtime.federation.governance.confirm(mapping.mapping_id, "test-auditor")
    catalog = runtime.federation.governance.service.catalog
    timestamp = datetime.now(UTC)
    catalog.save_snapshot(ScanSnapshot(
        id="scan-payments-v2", datasource_id="payments", version=2, status="ready",
        profile=DataSourceProfile(datasource_id="payments", name="payments",
                                  kind="relational", driver="sqlite"),
        started_at=timestamp, completed_at=timestamp, active=True,
    ))
    conversation = ConversationService(conversations).create(datasource_ids=["payments", "shipments"])
    run = runtime.create(conversation.conversation_id, "找出已付款但未发货的订单")
    assert runs.get(run.run_id).status == RunStatus.BLOCKED
    assert runs.get(run.run_id).failure_code == "stale_join_mapping"
    assert runtime.federation.repository.tasks(run.run_id) == []
