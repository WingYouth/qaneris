import pytest

from smartdata.graph import Neo4jConfig, NullGraphStore
from smartdata.graph.config import graph_store_from_environment


def test_neo4j_config_reads_all_supported_environment_values() -> None:
    config = Neo4jConfig.from_environment(
        {
            "SMARTDATA_NEO4J_URI": "neo4j://graph:7687",
            "SMARTDATA_NEO4J_USERNAME": "neo4j",
            "SMARTDATA_NEO4J_PASSWORD": "secret-value",
            "SMARTDATA_NEO4J_DATABASE": "smartdata",
        }
    )

    assert config.database == "smartdata"
    assert config.password == "secret-value"
    assert "secret-value" not in repr(config)


def test_partial_neo4j_configuration_never_falls_back_to_null_store() -> None:
    with pytest.raises(ValueError, match="SMARTDATA_NEO4J_USERNAME"):
        graph_store_from_environment({"SMARTDATA_NEO4J_URI": "neo4j://graph:7687"})


def test_null_store_requires_explicit_backend_selection() -> None:
    store = graph_store_from_environment({"SMARTDATA_GRAPH_STORE": "null"})

    assert isinstance(store, NullGraphStore)


def test_default_backend_requires_complete_neo4j_configuration() -> None:
    with pytest.raises(ValueError, match="Missing required Neo4j configuration"):
        graph_store_from_environment({})


def test_complete_configuration_constructs_neo4j_store(monkeypatch) -> None:
    captured = {}

    class Store:
        def __init__(self, uri, username, password, database):
            captured.update(uri=uri, username=username, password=password, database=database)

    monkeypatch.setattr("smartdata.graph.config.Neo4jGraphStore", Store)

    store = graph_store_from_environment(
        {
            "SMARTDATA_NEO4J_URI": "bolt://graph:7687",
            "SMARTDATA_NEO4J_USERNAME": "neo4j",
            "SMARTDATA_NEO4J_PASSWORD": "private",
            "SMARTDATA_NEO4J_DATABASE": "enterprise",
        }
    )

    assert isinstance(store, Store)
    assert captured == {
        "uri": "bolt://graph:7687",
        "username": "neo4j",
        "password": "private",
        "database": "enterprise",
    }
