from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi.testclient import TestClient

from qaneris.application.inventory import (
    is_selected_source_inventory_question,
    is_workspace_inventory_question,
)
from qaneris.application.service import QanerisService
from qaneris.catalog import Catalog
from qaneris.contracts import AskRequest, AskStatus, DatasetInfo, DatasourceCreate
from qaneris.contracts.profile import DataSourceProfile, ScanSnapshot, ScanStatus
from qaneris.interfaces.api.app import create_app
from qaneris.runtime.scheduler import InlineRunScheduler


def test_workspace_inventory_uses_only_the_requested_workspace_catalog(tmp_path) -> None:
    catalog = Catalog(tmp_path / "catalog.db")
    source = catalog.create_datasource(
        DatasourceCreate(
            name="销售库", kind="relational", connection={"driver": "sqlite", "path": ":memory:"},
            workspace_id="team-a",
        )
    )
    catalog.replace_datasets(
        source.id,
        [
            DatasetInfo(datasource_id=source.id, name="orders"),
            DatasetInfo(datasource_id=source.id, name="customers"),
        ],
    )
    catalog.create_datasource(
        DatasourceCreate(
            name="待扫描库", kind="relational", connection={"driver": "sqlite", "path": ":memory:"},
            workspace_id="team-a",
        )
    )
    catalog.create_datasource(
        DatasourceCreate(
            name="其他工作区", kind="relational", connection={"driver": "sqlite", "path": ":memory:"},
            workspace_id="team-b",
        )
    )
    service = QanerisService(catalog, model=Mock(), graph_store=Mock(), graph_reader=Mock())

    response = service.ask(
        AskRequest(question="当前工作区的都有哪些数据？", workspace_id="team-a")
    )

    assert response.status == AskStatus.COMPLETED
    assert response.analysis["result_kind"] == "workspace_inventory"
    assert "2 个数据源" in response.answer
    assert "2 个数据集" in response.answer
    assert {row["数据集"] for row in response.result.rows} == {"orders", "customers", "—"}
    assert all(row["数据源"] != "其他工作区" for row in response.result.rows)


def test_workspace_inventory_detector_does_not_capture_business_questions() -> None:
    assert is_workspace_inventory_question("当前工作区的都有哪些数据？")
    assert is_workspace_inventory_question("列出这个工作区的数据源")
    assert not is_workspace_inventory_question("当前工作区的销售额是多少？")


def test_selected_excel_inventory_bypasses_business_entity_confirmation(tmp_path) -> None:
    assert is_selected_source_inventory_question("当前excel是什么数据？")
    assert is_selected_source_inventory_question("这份 Excel 有哪些工作表？")
    assert not is_selected_source_inventory_question("当前 Excel 的销售额是多少？")

    catalog = Catalog(tmp_path / "catalog.db")
    source = catalog.create_datasource(
        DatasourceCreate(
            name="零售订单excel",
            kind="relational",
            connection={"driver": "sqlite", "path": ":memory:"},
        )
    )
    now = datetime.now(UTC)
    catalog.save_snapshot(
        ScanSnapshot(
            id="snapshot-1",
            datasource_id=source.id,
            version=1,
            status=ScanStatus.READY,
            profile=DataSourceProfile(
                datasource_id=source.id,
                name=source.name,
                kind=source.kind,
                driver="sqlite",
            ),
            started_at=now,
            completed_at=now,
            active=True,
        )
    )
    graph_reader = Mock()
    graph_reader.read_structure.return_value = SimpleNamespace(
        datasources=[SimpleNamespace(datasource_id=source.id, scan_version=1)],
        data_objects=[SimpleNamespace(node_id="orders", qualified_name="订单", name="订单", object_kind="table")],
        fields=[
            SimpleNamespace(object_id="orders", path="订单号"),
            SimpleNamespace(object_id="orders", path="event_time"),
        ],
        truncated=False,
    )
    model = Mock()
    model.summarize_schema_inventory.return_value = ""
    service = QanerisService(catalog, model=model, graph_store=Mock(), graph_reader=graph_reader)

    response = service.ask(
        AskRequest(question="当前excel是什么数据？", datasource_id=source.id)
    )

    assert response.status == AskStatus.COMPLETED
    assert response.analysis["result_kind"] == "schema_inventory"
    assert response.result.rows[0]["数据表"] == "订单"
    assert response.result.rows[0]["字段"] == "订单号、event_time（名称提示：事件时间）"
    assert "尚未确认业务含义" in response.answer

    other = catalog.create_datasource(
        DatasourceCreate(
            name="普通数据库",
            kind="relational",
            connection={"driver": "sqlite", "path": ":memory:"},
        )
    )
    catalog.replace_datasets(other.id, [DatasetInfo(datasource_id=other.id, name="other")])
    wrong_scope = service.ask(
        AskRequest(question="当前excel是什么数据？", datasource_id=other.id)
    )
    assert wrong_scope.status == AskStatus.CLARIFICATION_REQUIRED
    assert "选中目标 Excel" in wrong_scope.answer


def test_conversation_answers_workspace_inventory_with_multiple_sources(tmp_path) -> None:
    app = create_app(str(tmp_path / "catalog.db"))
    catalog = app.state.service.catalog
    for name in ("销售库", "用户库"):
        source = catalog.create_datasource(
            DatasourceCreate(
                name=name,
                kind="relational",
                connection={"driver": "sqlite", "path": ":memory:"},
            )
        )
        catalog.replace_datasets(source.id, [DatasetInfo(datasource_id=source.id, name="records")])
    app.state.run_orchestrator.scheduler = InlineRunScheduler()

    with TestClient(app) as client:
        conversation = client.post("/api/conversations", json={"datasource_ids": []}).json()
        created = client.post(
            f"/api/conversations/{conversation['conversation_id']}/runs",
            json={"question": "当前工作区的都有哪些数据？"},
        )
        assert created.status_code == 202
        run = client.get(f"/api/runs/{created.json()['run_id']}").json()

    assert run["status"] == "COMPLETED"
    assert run["response_json"]["analysis"]["result_kind"] == "workspace_inventory"
    assert "2 个数据源" in run["response_json"]["answer"]
