import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from qaneris.catalog import Catalog
from qaneris.contracts import (
    DatasetInfo,
    DatasourceCreate,
    FieldInfo,
    MappingInfo,
    RelationInfo,
)


def create_datasource(catalog: Catalog, name: str = "sales", workspace_id: str = "default") -> str:
    return catalog.create_datasource(
        DatasourceCreate(
            name=name,
            kind="relational",
            connection={"driver": "sqlite", "path": f"{name}.db"},
            workspace_id=workspace_id,
        )
    ).id


def test_initialization_adds_relation_and_mapping_tables_to_old_catalog(
    tmp_path: Path,
) -> None:
    catalog_path = tmp_path / "catalog.db"
    with sqlite3.connect(catalog_path) as connection:
        connection.executescript(
            """
            CREATE TABLE datasource (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                connection_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'created',
                UNIQUE(workspace_id, name)
            );
            CREATE TABLE dataset (
                id TEXT PRIMARY KEY,
                datasource_id TEXT NOT NULL REFERENCES datasource(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                UNIQUE(datasource_id, name)
            );
            INSERT INTO datasource VALUES (
                'ds_existing', 'default', 'existing', 'relational', '{}', 'ready'
            );
            INSERT INTO dataset VALUES (
                'set_existing', 'ds_existing', 'orders', 'table',
                '{"datasource_id":"ds_existing","name":"orders","kind":"table","fields":[]}'
            );
            """
        )

    catalog = Catalog(catalog_path)

    with sqlite3.connect(catalog_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert {"relation", "mapping"}.issubset(tables)
    assert [item.name for item in catalog.list_datasources()] == ["existing"]
    assert [item.name for item in catalog.list_datasets()] == ["orders"]


def test_models_round_trip_and_mapping_confidence_validation() -> None:
    relation = RelationInfo(
        datasource_id="ds_1",
        from_dataset="orders",
        from_field="customer_id",
        to_dataset="customers",
        to_field="id",
        relation_type="foreign_key",
    )
    mapping = MappingInfo(
        workspace_id="workspace_a",
        entity="customer",
        canonical_field="customer_id",
        datasource_id="ds_1",
        dataset="customers",
        field="id",
    )

    assert RelationInfo.model_validate_json(relation.model_dump_json()) == relation
    assert MappingInfo.model_validate_json(mapping.model_dump_json()) == mapping
    assert relation.source == "scan"
    assert mapping.confidence == 1.0
    assert mapping.source == "manual"
    with pytest.raises(ValidationError):
        MappingInfo.model_validate({**mapping.model_dump(), "confidence": 1.1})


def test_replace_scan_result_persists_datasets_relations_and_ready_status(
    tmp_path: Path,
) -> None:
    catalog = Catalog(tmp_path / "catalog.db")
    datasource_id = create_datasource(catalog)
    datasets = [
        DatasetInfo(
            datasource_id=datasource_id,
            name="customers",
            fields=[FieldInfo(name="id", data_type="INTEGER", primary_key=True)],
        ),
        DatasetInfo(
            datasource_id=datasource_id,
            name="orders",
            fields=[FieldInfo(name="customer_id", data_type="INTEGER")],
        ),
    ]
    relation = RelationInfo(
        datasource_id=datasource_id,
        from_dataset="orders",
        from_field="customer_id",
        to_dataset="customers",
        to_field="id",
        relation_type="foreign_key",
    )

    catalog.replace_scan_result(datasource_id, datasets, [relation])

    assert {item.name for item in catalog.list_datasets()} == {"customers", "orders"}
    assert catalog.list_relations() == [relation]
    assert catalog.get_datasource(datasource_id)[0].status == "ready"


def test_repeated_scan_replaces_old_relations(tmp_path: Path) -> None:
    catalog = Catalog(tmp_path / "catalog.db")
    datasource_id = create_datasource(catalog)
    dataset = DatasetInfo(datasource_id=datasource_id, name="orders")
    old_relation = RelationInfo(
        datasource_id=datasource_id,
        from_dataset="orders",
        to_dataset="customers",
        relation_type="foreign_key",
    )
    new_relation = RelationInfo(
        datasource_id=datasource_id,
        from_dataset="orders",
        to_dataset="accounts",
        relation_type="foreign_key",
    )

    catalog.replace_scan_result(datasource_id, [dataset], [old_relation])
    catalog.replace_scan_result(datasource_id, [dataset], [new_relation])

    assert catalog.list_relations(datasource_id=datasource_id) == [new_relation]


def test_failed_scan_result_is_rolled_back_without_marking_ready(tmp_path: Path) -> None:
    catalog = Catalog(tmp_path / "catalog.db")
    datasource_id = create_datasource(catalog)
    duplicate_datasets = [
        DatasetInfo(datasource_id=datasource_id, name="orders"),
        DatasetInfo(datasource_id=datasource_id, name="orders"),
    ]

    with pytest.raises(sqlite3.IntegrityError):
        catalog.replace_scan_result(datasource_id, duplicate_datasets, [])

    assert catalog.list_datasets() == []
    assert catalog.list_relations() == []
    assert catalog.get_datasource(datasource_id)[0].status == "created"


def test_relations_are_isolated_by_workspace(tmp_path: Path) -> None:
    catalog = Catalog(tmp_path / "catalog.db")
    first_datasource = create_datasource(catalog, "sales_a", "workspace_a")
    second_datasource = create_datasource(catalog, "sales_b", "workspace_b")
    first_relation = RelationInfo(
        datasource_id=first_datasource,
        from_dataset="orders",
        to_dataset="customers",
        relation_type="foreign_key",
    )
    second_relation = RelationInfo(
        datasource_id=second_datasource,
        from_dataset="orders",
        to_dataset="customers",
        relation_type="foreign_key",
    )
    catalog.replace_relations(first_datasource, [first_relation])
    catalog.replace_relations(second_datasource, [second_relation])

    assert catalog.list_relations("workspace_a") == [first_relation]
    assert catalog.list_relations("workspace_b") == [second_relation]


def test_mappings_are_persisted_filtered_and_workspace_isolated(tmp_path: Path) -> None:
    catalog = Catalog(tmp_path / "catalog.db")
    first_datasource = create_datasource(catalog, "sales_a", "workspace_a")
    second_datasource = create_datasource(catalog, "sales_b", "workspace_b")
    first = catalog.create_mapping(
        MappingInfo(
            workspace_id="workspace_a",
            entity="customer",
            canonical_field="customer_id",
            datasource_id=first_datasource,
            dataset="customers",
            field="id",
        )
    )
    catalog.create_mapping(
        MappingInfo(
            workspace_id="workspace_b",
            entity="product",
            canonical_field="product_id",
            datasource_id=second_datasource,
            dataset="products",
            field="id",
            confidence=0.8,
        )
    )

    assert first.id is not None and first.id.startswith("map_")
    assert catalog.list_mappings("workspace_a") == [first]
    assert catalog.list_mappings("workspace_a", entity="product") == []
    assert len(catalog.list_mappings("workspace_b")) == 1


def test_mapping_workspace_must_match_datasource(tmp_path: Path) -> None:
    catalog = Catalog(tmp_path / "catalog.db")
    datasource_id = create_datasource(catalog, workspace_id="workspace_a")

    with pytest.raises(ValueError, match="workspace"):
        catalog.create_mapping(
            MappingInfo(
                workspace_id="workspace_b",
                entity="customer",
                canonical_field="customer_id",
                datasource_id=datasource_id,
                dataset="customers",
                field="id",
            )
        )
