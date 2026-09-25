"""Application Service read methods for schema and datasource discovery (RS-MCP-01).

These methods exist so an interface layer - MCP today, Web next - can answer "what can this
installation connect to" and "what does this workspace look like" without holding a ``Catalog`` or
a model of its own. The tests pin two things: the data really comes back, and the service is the
only thing that had to know how to get it.

``summarize_schema`` gets extra attention because its fallback decision is a product rule: a
deployment with no model configured must still produce a summary rather than fail.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from smartdata.application.service import SmartDataService
from smartdata.catalog import Catalog
from smartdata.contracts import (
    DatasetInfo,
    DatasourceCreate,
    FieldInfo,
    MappingInfo,
    RelationInfo,
)


@pytest.fixture
def unconfigured(monkeypatch) -> None:
    """Make the deployment genuinely model-free.

    ``SmartDataService(model=None)`` means "not supplied", not "no model": the constructor then
    resolves one from the environment. A deployment with no model configured is the state where
    ``from_environment`` itself returns ``None``, so that is what these tests reproduce.
    """
    monkeypatch.setattr(
        "smartdata.application.service.AiyallmSchemaModel.from_environment",
        classmethod(lambda cls: None),
    )


def seed(instance: SmartDataService, name: str = "sales") -> str:
    """Register one real SQLite datasource, so the create path runs for real.

    The connection test in ``create_datasource`` opens the file, so the workspace database is
    materialized rather than faked: these tests read a catalog whose contents were produced the same
    way production ones are.
    """
    database = Path(instance.catalog.path).parent / f"{name}.db"
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE "orders" (id INTEGER PRIMARY KEY, amount REAL)')
    datasource = instance.create_datasource(
        DatasourceCreate(
            name=name,
            kind="relational",
            connection={"driver": "sqlite", "path": str(database)},
        )
    )
    return datasource.id


def test_list_supported_adapters_reports_the_registry(unconfigured) -> None:
    adapters = SmartDataService(Catalog(":memory:")).list_supported_adapters()

    assert adapters, "the installation registers adapters"
    by_driver = {item["driver"]: item for item in adapters}
    assert "sqlite" in by_driver
    assert by_driver["sqlite"]["kind"] == "relational"
    # Sorted and stable, so a caller can diff two installations.
    assert adapters == sorted(adapters, key=lambda item: (item["kind"], item["driver"]))
    for item in adapters:
        assert set(item) >= {"kind", "driver", "query_language", "connection_example"}


def test_get_schema_context_returns_the_normalized_structure(tmp_path, unconfigured) -> None:
    instance = SmartDataService(Catalog(tmp_path / "catalog.db"), model=None)
    datasource_id = seed(instance)

    context = instance.get_schema_context("default")

    assert context["workspace_id"] == "default"
    assert [item["id"] for item in context["datasources"]] == [datasource_id]
    assert set(context) == {
        "workspace_id",
        "datasources",
        "datasets",
        "relations",
        "samples",
        "mapping_candidates",
    }


def test_get_schema_context_is_scoped_to_the_workspace(tmp_path, unconfigured) -> None:
    instance = SmartDataService(Catalog(tmp_path / "catalog.db"), model=None)
    seed(instance, "sales")

    assert instance.get_schema_context("default")["datasources"]
    assert instance.get_schema_context("other")["datasources"] == []


def test_search_datasets_matches_dataset_and_field_names(tmp_path, unconfigured) -> None:
    instance = SmartDataService(Catalog(tmp_path / "catalog.db"), model=None)
    datasource_id = seed(instance)

    # Published datasets normally come from a scan; store the structures directly so the search
    # itself is what is under test here.
    instance.catalog.replace_datasets(
        datasource_id,
        [
            DatasetInfo(
                datasource_id=datasource_id,
                name="orders",
                kind="table",
                fields=[FieldInfo(name="total_amount", data_type="REAL")],
            ),
            DatasetInfo(
                datasource_id=datasource_id,
                name="customers",
                kind="table",
                fields=[FieldInfo(name="region", data_type="TEXT")],
            ),
        ],
    )

    assert [item.name for item in instance.search_datasets("order")] == ["orders"]
    # A field-name match returns the dataset that holds it.
    assert [item.name for item in instance.search_datasets("region")] == ["customers"]
    assert [item.name for item in instance.search_datasets("ORDER")] == ["orders"]
    assert instance.search_datasets("nothing_matches") == []


def test_search_datasets_is_scoped_to_the_workspace(tmp_path, unconfigured) -> None:
    instance = SmartDataService(Catalog(tmp_path / "catalog.db"), model=None)
    datasource_id = seed(instance)

    instance.catalog.replace_datasets(
        datasource_id,
        [DatasetInfo(datasource_id=datasource_id, name="orders", kind="table", fields=[])],
    )

    assert instance.search_datasets("orders", "default")
    assert instance.search_datasets("orders", "other") == []


def test_list_relations_and_mappings_round_trip(tmp_path, unconfigured) -> None:
    instance = SmartDataService(Catalog(tmp_path / "catalog.db"), model=None)
    datasource_id = seed(instance)
    relation = RelationInfo(
        datasource_id=datasource_id,
        from_dataset="orders",
        to_dataset="customers",
        relation_type="foreign_key",
    )
    mapping = MappingInfo(
        entity="customer",
        canonical_field="customer_id",
        datasource_id=datasource_id,
        dataset="customers",
        field="id",
    )
    instance.catalog.replace_relations(datasource_id, [relation])
    stored_mapping = instance.catalog.create_mapping(mapping)

    assert instance.list_relations("default") == [relation]
    assert instance.list_relations("default", datasource_id) == [relation]
    assert instance.list_relations("default", "ds_other") == []
    assert instance.list_mappings("default") == [stored_mapping]
    assert instance.list_mappings("default", entity="customer") == [stored_mapping]
    assert instance.list_mappings("default", entity="no_such_entity") == []


def test_summarize_schema_falls_back_without_a_model(tmp_path, unconfigured) -> None:
    instance = SmartDataService(Catalog(tmp_path / "catalog.db"), model=None)
    seed(instance)

    summary = instance.summarize_schema("default")

    assert isinstance(summary, str)
    assert "1" in summary, "the deterministic fallback counts the workspace contents"


def test_summarize_schema_uses_the_configured_model(tmp_path, unconfigured) -> None:
    class RecordingModel:
        def __init__(self) -> None:
            self.contexts: list[dict[str, Any]] = []

        def summarize_schema(self, context: dict[str, Any]) -> str:
            self.contexts.append(context)
            return "model summary"

    model = RecordingModel()
    instance = SmartDataService(Catalog(tmp_path / "catalog.db"), model=model)
    seed(instance)

    assert instance.summarize_schema("default") == "model summary"
    assert len(model.contexts) == 1
    # The model receives exactly the public context the read method returns.
    assert model.contexts[0] == instance.get_schema_context("default")
