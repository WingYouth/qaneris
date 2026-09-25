from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, Self

import pytest

from qaneris.contracts import DatasetInfo, Datasource, FieldInfo, RelationInfo
from qaneris.graph.neo4j import Neo4jGraphStore
from qaneris.scan import ScanEdgeType, ScanGraphBuilder


class _Result:
    def consume(self) -> None:
        return None


class _Transaction:
    def __init__(self, state: dict[str, Any], fail_after: int | None = None):
        self.state = state
        self.fail_after = fail_after
        self.operations = 0

    def run(self, query: str, **parameters: Any) -> _Result:
        self.operations += 1
        if self.fail_after is not None and self.operations > self.fail_after:
            raise RuntimeError("simulated Neo4j transaction failure")
        if query.startswith("MATCH (d:Database"):
            datasource_id = parameters["datasource_id"]
            owned = {
                identifier
                for identifier, node in self.state["nodes"].items()
                if node.get("label") == "Database" and node.get("datasource_id") == datasource_id
            }
            while True:
                discovered = {
                    edge["to"] for edge in self.state["edges"].values() if edge["from"] in owned
                }
                if discovered <= owned:
                    break
                owned.update(discovered)
            self.state["nodes"] = {
                identifier: node
                for identifier, node in self.state["nodes"].items()
                if identifier not in owned
            }
            self.state["edges"] = {
                identifier: edge
                for identifier, edge in self.state["edges"].items()
                if edge["from"] not in owned and edge["to"] not in owned
            }
        elif query.startswith("CREATE (n:"):
            label = query.split("CREATE (n:", 1)[1].split(" ", 1)[0]
            self.state["nodes"][parameters["id"]] = {
                "label": label,
                "id": parameters["id"],
                **parameters["properties"],
            }
        elif query.startswith("MATCH (a"):
            edge_type = query.split("CREATE (a)-[r:", 1)[1].split(" ", 1)[0]
            self.state["edges"][parameters["id"]] = {
                "type": edge_type,
                "from": parameters["from_id"],
                "to": parameters["to_id"],
                **parameters["properties"],
            }
        return _Result()


class _Session:
    def __init__(self, driver: _Driver):
        self.driver = driver

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def run(self, query: str) -> _Result:
        self.driver.schema_statements.add(query)
        return _Result()

    def execute_write(self, callback, *args):
        candidate = deepcopy(self.driver.state)
        transaction = _Transaction(candidate, self.driver.fail_after)
        callback(transaction, *args)
        self.driver.state = candidate


class _Driver:
    def __init__(self):
        self.state: dict[str, Any] = {"nodes": {}, "edges": {}}
        self.schema_statements: set[str] = set()
        self.fail_after: int | None = None

    def session(self, **_: object) -> _Session:
        return _Session(self)


def _store(driver: _Driver) -> Neo4jGraphStore:
    store = object.__new__(Neo4jGraphStore)
    store._driver = driver
    store._database = "neo4j"
    return store


def _datasource() -> Datasource:
    return Datasource(
        id="sales", name="Sales", kind="relational", workspace_id="default", driver="sqlite"
    )


def _graph(version: int, datasets: list[DatasetInfo], relations: list[RelationInfo]):
    return ScanGraphBuilder().build(
        _datasource(), datasets, relations, version=version, scanned_at=datetime.now(UTC)
    )


def test_replace_datasource_graph_replaces_the_complete_current_subgraph() -> None:
    driver = _Driver()
    store = _store(driver)
    first = _graph(
        1,
        [
            DatasetInfo(
                datasource_id="sales",
                name="orders",
                fields=[
                    FieldInfo(name="id", data_type="integer"),
                    FieldInfo(name="customer_id", data_type="integer"),
                ],
            ),
            DatasetInfo(
                datasource_id="sales",
                name="customers",
                fields=[FieldInfo(name="id", data_type="integer")],
            ),
        ],
        [
            RelationInfo(
                datasource_id="sales",
                from_dataset="orders",
                from_field="customer_id",
                to_dataset="customers",
                to_field="id",
                relation_type="foreign_key",
                source="scan",
            )
        ],
    )
    second = _graph(
        2,
        [
            DatasetInfo(
                datasource_id="sales",
                name="orders",
                fields=[
                    FieldInfo(name="id", data_type="integer"),
                    FieldInfo(name="status", data_type="text"),
                ],
            ),
            DatasetInfo(
                datasource_id="sales",
                name="products",
                fields=[FieldInfo(name="id", data_type="integer")],
            ),
        ],
        [],
    )

    store.replace_datasource_graph(first)
    store.replace_datasource_graph(second)

    nodes = list(driver.state["nodes"].values())
    object_names = {node["name"] for node in nodes if node["label"] == "DataObject"}
    field_names = {node["name"] for node in nodes if node["label"] == "Field"}
    assert object_names == {"orders", "products"}
    assert field_names == {"id", "status"}
    assert all(
        edge["type"] != ScanEdgeType.RELATES_TO.value for edge in driver.state["edges"].values()
    )
    assert len(driver.state["nodes"]) == len(set(driver.state["nodes"]))
    assert all(
        edge["from"] in driver.state["nodes"] and edge["to"] in driver.state["nodes"]
        for edge in driver.state["edges"].values()
    )


def test_transaction_failure_preserves_the_previous_graph() -> None:
    driver = _Driver()
    store = _store(driver)
    first = _graph(1, [DatasetInfo(datasource_id="sales", name="orders")], [])
    second = _graph(2, [DatasetInfo(datasource_id="sales", name="products")], [])
    store.replace_datasource_graph(first)
    before = deepcopy(driver.state)
    driver.fail_after = 1

    with pytest.raises(RuntimeError, match="transaction failure"):
        store.replace_datasource_graph(second)

    assert driver.state == before


def test_ensure_schema_can_be_called_twice() -> None:
    driver = _Driver()
    store = _store(driver)

    store.ensure_schema()
    first = set(driver.schema_statements)
    store.ensure_schema()

    assert driver.schema_statements == first
    assert first
