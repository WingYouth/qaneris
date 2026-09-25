from qaneris.graph.schema import GRAPH_INDEXES, NODE_CONSTRAINTS


def test_every_scan_node_label_has_an_id_uniqueness_constraint() -> None:
    for label in ("Database", "Namespace", "DataObject", "Field", "Index", "Constraint"):
        assert any(
            f"FOR (n:{label}) REQUIRE n.id IS UNIQUE" in statement for statement in NODE_CONSTRAINTS
        )


def test_required_lookup_indexes_are_declared() -> None:
    assert any(
        "DataObject" in statement and "datasource_id" in statement for statement in GRAPH_INDEXES
    )
    assert any(
        "Database" in statement and "workspace_id" in statement for statement in GRAPH_INDEXES
    )


def test_schema_statements_are_idempotent() -> None:
    assert all("IF NOT EXISTS" in statement for statement in (*NODE_CONSTRAINTS, *GRAPH_INDEXES))
