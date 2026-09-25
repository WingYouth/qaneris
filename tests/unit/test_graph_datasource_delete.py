"""Graph datasource deletion (RS-CONN-01A).

Deleting a datasource must publish the removal through the same boundary that published the graph,
and it must remove exactly one datasource. These tests pin the two properties that make that safe:

* the datasource id is only ever a bound parameter, never interpolated into the Cypher - a graph
  store that builds statements from input is one refactor away from a query-injection bug; and
* ownership determines the blast radius - deleting one datasource leaves every other published
  datasource, its objects and its fields in place.

The fake driver records the exact statements and parameters it was handed and applies the same
ownership traversal the real store does, so a change to the query is visible to a test rather than
hidden behind a mock that always answers yes. Graphs are produced by the real ``ScanGraphBuilder``,
so the node and edge shape under test is the shape the product actually publishes.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime

import pytest

from smartdata.contracts import DatasetInfo, Datasource, FieldInfo
from smartdata.contracts.datasource import DatasourceKind
from smartdata.graph import Neo4jGraphStore
from smartdata.graph.ports import NullGraphStore
from smartdata.scan import ScanGraphBuilder
from tests.integration._neo4j_fake import FakeNeo4jDriver
from tests.integration._neo4j_fake import graph_store as _store_on

_DATASOURCE_IDS = "ds_alpha", "ds_beta"


def _datasource(datasource_id: str) -> Datasource:
    return Datasource(
        id=datasource_id,
        name=datasource_id,
        kind=DatasourceKind.RELATIONAL,
        workspace_id="default",
        driver="sqlite",
    )


def _graph(datasource_id: str):
    """One datasource owning a table with one field, built the way a real scan builds it."""
    return ScanGraphBuilder().build(
        _datasource(datasource_id),
        [
            DatasetInfo(
                datasource_id=datasource_id,
                name=f"{datasource_id}_orders",
                kind="table",
                fields=[FieldInfo(name="id", data_type="INTEGER")],
            )
        ],
        [],
        version=1,
        scanned_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _publish(driver: FakeNeo4jDriver, datasource_id: str) -> None:
    _store_on(driver).replace_datasource_graph(_graph(datasource_id))


def _seeded() -> FakeNeo4jDriver:
    driver = FakeNeo4jDriver()
    for datasource_id in _DATASOURCE_IDS:
        _publish(driver, datasource_id)
    return driver


def _owned_node_ids(driver: FakeNeo4jDriver, datasource_id: str) -> set[str]:
    return {
        identifier
        for identifier, node in driver.nodes.items()
        if node.get("datasource_id") == datasource_id
    }


# --------------------------------------------------------------------------------------------
# the query is parameterized
# --------------------------------------------------------------------------------------------


def test_delete_binds_the_datasource_id_as_a_parameter() -> None:
    """The identifier is a query parameter, so a hostile id cannot become Cypher."""
    driver = FakeNeo4jDriver()
    _publish(driver, "ds_alpha")
    driver.writes.clear()

    _store_on(driver).delete_datasource_graph("ds_alpha")

    assert len(driver.writes) == 1
    statement, parameters = driver.writes[0]
    assert parameters == {"datasource_id": "ds_alpha"}
    assert "$datasource_id" in statement


def test_a_traversal_sensitive_id_is_passed_as_data() -> None:
    """An id containing Cypher is treated as a string, never parsed as a statement."""
    driver = FakeNeo4jDriver()
    hostile = "ds_alpha'}) MATCH (n) DETACH DELETE n //"

    _store_on(driver).delete_datasource_graph(hostile)

    statement, parameters = driver.writes[0]
    assert parameters["datasource_id"] == hostile
    assert hostile not in statement


def test_delete_uses_the_ownership_traversal_shared_with_publication() -> None:
    driver = FakeNeo4jDriver()

    _store_on(driver).delete_datasource_graph("ds_alpha")

    statement, _ = driver.writes[0]
    assert statement.startswith("MATCH (d:Database {datasource_id: $datasource_id})")
    assert "OPTIONAL MATCH (d)-[*0..]->(owned)" in statement
    assert "WITH DISTINCT owned" in statement
    assert "DETACH DELETE owned" in statement


def test_publication_and_deletion_use_the_same_delete_statement() -> None:
    """A drift between "clear the old revision" and "remove the datasource" is a bug waiting."""
    published = FakeNeo4jDriver()
    _publish(published, "ds_alpha")
    publish_delete = next(row[0] for row in published.writes if "$datasource_id" in row[0])

    deleting = FakeNeo4jDriver()
    _store_on(deleting).delete_datasource_graph("ds_alpha")

    assert deleting.writes[0][0] == publish_delete


# --------------------------------------------------------------------------------------------
# only the selected datasource is removed
# --------------------------------------------------------------------------------------------


def test_delete_removes_only_the_selected_datasource_graph() -> None:
    driver = _seeded()
    alpha = _owned_node_ids(driver, "ds_alpha")
    beta = _owned_node_ids(driver, "ds_beta")
    assert alpha and beta and alpha.isdisjoint(beta)

    _store_on(driver).delete_datasource_graph("ds_alpha")

    assert _owned_node_ids(driver, "ds_alpha") == set()
    assert _owned_node_ids(driver, "ds_beta") == beta


def test_delete_leaves_the_other_datasources_edges_intact() -> None:
    driver = _seeded()
    beta_nodes = _owned_node_ids(driver, "ds_beta")
    beta_edges = {
        identifier
        for identifier, edge in driver.edges.items()
        if edge["from"] in beta_nodes or edge["to"] in beta_nodes
    }
    assert beta_edges

    _store_on(driver).delete_datasource_graph("ds_alpha")

    remaining = set(driver.edges)
    assert beta_edges <= remaining


def test_delete_removes_the_whole_owned_subtree() -> None:
    """Fields hang off data objects, so a field must not outlive its datasource.

    Field nodes carry no ``datasource_id`` of their own - they are reached through ownership - so
    this checks them by label rather than by direct attribution.
    """
    driver = _seeded()
    alpha_objects = {
        node["id"]
        for node in driver.nodes.values()
        if node.get("label") == "DataObject" and node.get("datasource_id") == "ds_alpha"
    }
    alpha_fields = {
        node["id"]
        for node in driver.nodes.values()
        if node.get("label") == "Field" and node.get("object_id") in alpha_objects
    }
    assert alpha_fields

    _store_on(driver).delete_datasource_graph("ds_alpha")

    assert alpha_fields.isdisjoint(driver.nodes)


def test_delete_of_an_unpublished_datasource_is_a_no_op() -> None:
    """A datasource with nothing published is still a valid deletion, not an error."""
    driver = _seeded()
    nodes, edges = dict(driver.nodes), dict(driver.edges)

    _store_on(driver).delete_datasource_graph("ds_missing")

    assert driver.nodes == nodes
    assert driver.edges == edges


def test_deleting_each_datasource_leaves_an_empty_graph() -> None:
    driver = _seeded()

    for datasource_id in _DATASOURCE_IDS:
        _store_on(driver).delete_datasource_graph(datasource_id)

    assert driver.nodes == {}
    assert driver.edges == {}


# --------------------------------------------------------------------------------------------
# failure behavior
# --------------------------------------------------------------------------------------------


def test_a_store_failure_propagates_rather_than_being_swallowed() -> None:
    """Deletion failure must be visible: the caller aborts the datasource deletion on it."""
    driver = _seeded()
    driver.fail_after = 0

    with pytest.raises(RuntimeError, match="simulated Neo4j transaction failure"):
        _store_on(driver).delete_datasource_graph("ds_alpha")


def test_a_failed_delete_leaves_the_graph_unchanged() -> None:
    """Deletion runs in one transaction, so a failure commits nothing."""
    driver = _seeded()
    nodes, edges = dict(driver.nodes), dict(driver.edges)
    driver.fail_after = 0

    with pytest.raises(RuntimeError):
        _store_on(driver).delete_datasource_graph("ds_alpha")

    assert driver.nodes == nodes
    assert driver.edges == edges


# --------------------------------------------------------------------------------------------
# compatibility store and port conformance
# --------------------------------------------------------------------------------------------


def test_null_graph_store_delete_is_a_no_op() -> None:
    """The compatibility store accepts a deletion without inventing behavior for it."""
    assert NullGraphStore().delete_datasource_graph("ds_alpha") is None


def test_the_real_store_implements_the_port_method() -> None:
    """``Neo4jGraphStore`` really implements the port method, not just a similarly named one."""
    assert callable(Neo4jGraphStore.delete_datasource_graph)
    signature = inspect.signature(Neo4jGraphStore.delete_datasource_graph)
    assert list(signature.parameters) == ["self", "datasource_id"]


def test_the_port_and_the_store_agree_on_the_signature() -> None:
    """The Protocol method and the implementation must stay callable the same way."""
    from smartdata.graph.ports import GraphStore

    port = inspect.signature(GraphStore.delete_datasource_graph)
    implementation = inspect.signature(Neo4jGraphStore.delete_datasource_graph)
    assert list(port.parameters) == list(implementation.parameters)


def test_delete_is_removal_only() -> None:
    """Deletion removes owned nodes and edges and creates nothing."""
    driver = FakeNeo4jDriver()
    _publish(driver, "ds_alpha")
    before_nodes, before_edges = len(driver.nodes), len(driver.edges)
    driver.writes.clear()

    _store_on(driver).delete_datasource_graph("ds_alpha")

    assert len(driver.nodes) < before_nodes
    assert len(driver.edges) < before_edges
    assert all("CREATE" not in statement for statement, _ in driver.writes)
