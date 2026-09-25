import pytest

from qaneris.graph import Neo4jConfig, NullGraphStore
from qaneris.graph.config import graph_store_from_environment


def test_neo4j_config_reads_all_supported_environment_values() -> None:
    config = Neo4jConfig.from_environment(
        {
            "QANERIS_NEO4J_URI": "neo4j://graph:7687",
            "QANERIS_NEO4J_USERNAME": "neo4j",
            "QANERIS_NEO4J_PASSWORD": "secret-value",
            "QANERIS_NEO4J_DATABASE": "qaneris",
        }
    )

    assert config.database == "qaneris"
    assert config.password == "secret-value"
    assert "secret-value" not in repr(config)


def test_partial_neo4j_configuration_never_falls_back_to_null_store() -> None:
    with pytest.raises(ValueError, match="QANERIS_NEO4J_USERNAME"):
        graph_store_from_environment({"QANERIS_NEO4J_URI": "neo4j://graph:7687"})


def test_null_store_requires_explicit_backend_selection() -> None:
    store = graph_store_from_environment({"QANERIS_GRAPH_STORE": "null"})

    assert isinstance(store, NullGraphStore)


def test_default_backend_requires_complete_neo4j_configuration() -> None:
    with pytest.raises(ValueError, match="Missing required Neo4j configuration"):
        graph_store_from_environment({})


def test_complete_configuration_constructs_neo4j_store(monkeypatch) -> None:
    captured = {}

    class Store:
        def __init__(self, uri, username, password, database):
            captured.update(uri=uri, username=username, password=password, database=database)

    monkeypatch.setattr("qaneris.graph.config.Neo4jGraphStore", Store)

    store = graph_store_from_environment(
        {
            "QANERIS_NEO4J_URI": "bolt://graph:7687",
            "QANERIS_NEO4J_USERNAME": "neo4j",
            "QANERIS_NEO4J_PASSWORD": "private",
            "QANERIS_NEO4J_DATABASE": "enterprise",
        }
    )

    assert isinstance(store, Store)
    assert captured == {
        "uri": "bolt://graph:7687",
        "username": "neo4j",
        "password": "private",
        "database": "enterprise",
    }
