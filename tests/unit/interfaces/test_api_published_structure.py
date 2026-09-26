"""The published-structure endpoint that makes JoinMapping node ids selectable.

A mapping is confirmed against the published graph, so the only identifiers that can be confirmed
are the graph's own node ids - which the catalog does not hold. These tests pin the projection that
exposes them, and the behaviour that keeps it from looking like an empty schema.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from qaneris.common.errors import GraphUnavailableError
from qaneris.graph.reading import GraphDataObject, GraphDatasource, GraphField, GraphStructure
from qaneris.interfaces.api import app as api_app
from qaneris.interfaces.api.app import create_app


def structure() -> GraphStructure:
    return GraphStructure(
        datasources=[
            GraphDatasource(node_id="db_1", datasource_id="ds_1", workspace_id="default",
                            name="订单库", kind="relational", driver="sqlite", scan_version=1)
        ],
        data_objects=[
            GraphDataObject(node_id="obj_1", datasource_id="ds_1", name="orders", object_kind="table"),
            GraphDataObject(node_id="obj_2", datasource_id="ds_1", name="customers", object_kind="table"),
        ],
        fields=[
            GraphField(node_id="f_1", object_id="obj_1", datasource_id="ds_1",
                       object_name="orders", name="customer_id", path="customer_id", data_type="INTEGER"),
            GraphField(node_id="f_2", object_id="obj_2", datasource_id="ds_1",
                       object_name="customers", name="id", path="id", data_type="INTEGER"),
        ],
    )


def service_stub() -> SimpleNamespace:
    """A service double exposing only what the route is allowed to touch."""

    def describe(workspace_id: str = "default", datasource_id: str | None = None):
        return {
            "workspace_id": workspace_id,
            "objects": [
                {
                    "node_id": "obj_1",
                    "datasource_id": "ds_1",
                    "datasource_name": "订单库",
                    "name": "orders",
                    "fields": [{"node_id": "f_1", "path": "customer_id", "data_type": "INTEGER"}],
                }
            ],
            "truncated": False,
        }

    return SimpleNamespace(describe_published_structure=describe)


def test_the_endpoint_returns_selectable_node_ids(tmp_path, monkeypatch) -> None:
    # ``create_app`` composes its own service and this route closes over it, so the double is
    # installed at the same composition seam every other synchronous route test uses.
    monkeypatch.setattr(api_app, "QanerisService", lambda *args, **kwargs: service_stub())
    client = TestClient(create_app(database_path=str(tmp_path / "catalog.db")))

    response = client.get(
        "/api/governance/published-structure", params={"workspace_id": "default"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["objects"][0]["node_id"] == "obj_1"
    assert body["objects"][0]["fields"][0]["node_id"] == "f_1"


def test_an_unreachable_graph_is_reported_rather_than_shown_as_empty() -> None:
    """An empty object list would render as "this workspace has no tables" - a different fact."""
    from qaneris.application.service import QanerisService

    class FailingReader:
        def read_structure(self, request):
            raise GraphUnavailableError("图后端不可用：无法从 Neo4j 企业数据图读取结构")

    service = QanerisService.__new__(QanerisService)
    service.graph_reader = FailingReader()
    service.catalog = SimpleNamespace(list_datasources=lambda workspace_id="default": [])
    with pytest.raises(GraphUnavailableError):
        QanerisService.describe_published_structure(service, "default")


def test_a_datasource_outside_the_workspace_is_refused() -> None:
    from qaneris.application.service import QanerisService

    service = QanerisService.__new__(QanerisService)
    service.catalog = SimpleNamespace(
        get_datasource=lambda datasource_id: (SimpleNamespace(id=datasource_id, workspace_id="other"), None)
    )
    service.graph_reader = SimpleNamespace(read_structure=lambda request: structure())

    with pytest.raises(ValueError):
        QanerisService.describe_published_structure(service, "default", "ds_1")


def test_the_projection_is_built_from_the_graph_not_the_catalog() -> None:
    """Node ids exist only in the graph; a catalog-backed answer could not carry them."""
    from qaneris.application.service import QanerisService

    service = QanerisService.__new__(QanerisService)
    service.catalog = SimpleNamespace(
        list_datasources=lambda workspace_id="default": [
            SimpleNamespace(id="ds_1", name="订单库")
        ]
    )
    service.graph_reader = SimpleNamespace(read_structure=lambda request: structure())

    body = QanerisService.describe_published_structure(service, "default")

    assert {item["node_id"] for item in body["objects"]} == {"obj_1", "obj_2"}
    assert all(item["datasource_name"] == "订单库" for item in body["objects"])
    assert body["truncated"] is False
    # Every field is nested under the object it belongs to; a field never leaks across objects.
    orders = next(item for item in body["objects"] if item["name"] == "orders")
    assert [field["node_id"] for field in orders["fields"]] == ["f_1"]
    customers = next(item for item in body["objects"] if item["name"] == "customers")
    assert [field["node_id"] for field in customers["fields"]] == ["f_2"]
