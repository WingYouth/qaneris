from pathlib import Path

from qaneris.catalog import Catalog
from qaneris.contracts import DatasetInfo, DatasetSample, DatasourceCreate, FieldInfo
from qaneris.querying.retrieval.schema_context import build_schema_context


def _source(catalog: Catalog, name: str):
    return catalog.create_datasource(
        DatasourceCreate(
            name=name,
            kind="relational",
            connection={"driver": "sqlite", "path": f"{name}.db"},
        )
    )


def test_schema_context_infers_cross_source_field_candidates(tmp_path: Path) -> None:
    catalog = Catalog(tmp_path / "catalog.db")
    first = _source(catalog, "first")
    second = _source(catalog, "second")
    first_dataset = DatasetInfo(
        datasource_id=first.id,
        name="customers",
        fields=[FieldInfo(name="customer_id", data_type="INTEGER")],
    )
    second_dataset = DatasetInfo(
        datasource_id=second.id,
        name="profiles",
        fields=[FieldInfo(name="customer_id", data_type="long")],
    )
    catalog.replace_scan_result(
        first.id,
        [first_dataset],
        [],
        [DatasetSample(datasource_id=first.id, dataset="customers", rows=[{"customer_id": 1}])],
    )
    catalog.replace_scan_result(
        second.id,
        [second_dataset],
        [],
        [DatasetSample(datasource_id=second.id, dataset="profiles", rows=[{"customer_id": 1}])],
    )

    context = build_schema_context(catalog)

    assert context["mapping_candidates"] == [
        {
            "canonical_field": "customer_id",
            "confidence": 0.8,
            "source": "exact_field_name",
            "fields": [
                {
                    "datasource_id": first.id,
                    "dataset": "customers",
                    "field": "customer_id",
                    "data_type": "INTEGER",
                },
                {
                    "datasource_id": second.id,
                    "dataset": "profiles",
                    "field": "customer_id",
                    "data_type": "long",
                },
            ],
        }
    ]


def test_dataset_sample_rejects_more_than_three_rows() -> None:
    try:
        DatasetSample(
            datasource_id="ds_1",
            dataset="orders",
            rows=[{"id": index} for index in range(4)],
        )
    except ValueError:
        return
    raise AssertionError("DatasetSample must reject more than three rows")
