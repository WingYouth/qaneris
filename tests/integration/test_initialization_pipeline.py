from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from qaneris.catalog import Catalog
from qaneris.contracts import (
    AuthenticationConfig,
    AuthenticationMethod,
    CompanyDataProfile,
    ConnectionEndpoint,
    ConnectionProfile,
    DataObjectKind,
    DataObjectProfile,
    DatasetInfo,
    DatasetSample,
    Datasource,
    DatasourceCreate,
    DatasourceKind,
    DataSourceProfile,
    DocumentStatus,
    FieldProfile,
    MetadataOrigin,
    NamespaceProfile,
    RelationshipProfile,
    SamplePreview,
    ScanPolicy,
    ScanSnapshot,
    ScanStatus,
    SecretProviderKind,
    SecretReference,
    SecureDatasourceCreate,
)
from qaneris.initialization import DatabaseInitializer
from qaneris.profiling import ProfileBuilder
from qaneris.querying import QueryPreparationPipeline
from qaneris.querying.retrieval import ProfileRetriever, RuleBasedQueryIntentParser


def test_secure_connection_profile_persists_only_secret_reference(tmp_path) -> None:
    catalog_path = tmp_path / "catalog.db"
    catalog = Catalog(catalog_path)
    datasource = catalog.create_secure_datasource(
        SecureDatasourceCreate(
            name="sales",
            kind=DatasourceKind.RELATIONAL,
            connection_profile=ConnectionProfile(
                driver="postgresql",
                deployment_mode="cloud",
                endpoint=ConnectionEndpoint(
                    hosts=[{"host": "db.example", "port": 5432}], database="sales"
                ),
                authentication=AuthenticationConfig(
                    method=AuthenticationMethod.PASSWORD,
                    username="readonly",
                    password=SecretReference(
                        provider=SecretProviderKind.ENVIRONMENT,
                        identifier="SALES_DB_PASSWORD",
                    ),
                ),
            ),
        )
    )

    with sqlite3.connect(catalog_path) as connection:
        stored = connection.execute(
            "SELECT connection_json FROM datasource WHERE id=?", (datasource.id,)
        ).fetchone()[0]

    assert "SALES_DB_PASSWORD" in stored
    assert "readonly" in stored
    assert '"password": "secret"' not in stored


def test_catalog_rejects_inline_secret_even_when_called_directly(tmp_path) -> None:
    catalog = Catalog(tmp_path / "catalog.db")

    with pytest.raises(ValueError, match="inline connection secrets"):
        catalog.create_datasource(
            DatasourceCreate(
                name="unsafe",
                kind="relational",
                connection={"driver": "postgresql", "password": "plaintext"},
            )
        )


def test_catalog_rejects_password_embedded_in_connection_url(tmp_path) -> None:
    catalog = Catalog(tmp_path / "catalog.db")

    with pytest.raises(ValueError, match="inline connection secrets"):
        catalog.create_datasource(
            DatasourceCreate(
                name="unsafe-url",
                kind="relational",
                connection={
                    "driver": "postgresql",
                    "url": "postgresql+psycopg://readonly:plaintext@localhost/retail",
                },
            )
        )


def test_initialization_job_rejects_invalid_state_transition(tmp_path) -> None:
    catalog = Catalog(tmp_path / "catalog.db")
    datasource = catalog.create_secure_datasource(
        SecureDatasourceCreate(
            name="sales",
            kind="relational",
            connection_profile=ConnectionProfile(
                driver="sqlite", endpoint=ConnectionEndpoint(path="sales.db")
            ),
        )
    )
    job = catalog.create_initialization_job(datasource.id)

    with pytest.raises(ValueError, match="created -> ready"):
        catalog.update_initialization_job(job.id, ScanStatus.READY)


def test_document_failure_keeps_scan_ready_and_can_be_retried(tmp_path) -> None:
    source_path = tmp_path / "source.db"
    with sqlite3.connect(source_path) as connection:
        connection.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, amount REAL)")
        connection.execute("INSERT INTO orders VALUES (1, 10.5)")

    catalog = Catalog(tmp_path / "catalog.db")
    datasource = catalog.create_datasource(
        DatasourceCreate(
            name="sales",
            kind="relational",
            connection={"driver": "sqlite", "path": str(source_path)},
        )
    )

    class FailingDocumentWriter:
        def write(self, snapshot, workspace_id):
            raise OSError("document storage unavailable")

    job = DatabaseInitializer(catalog, document_writer=FailingDocumentWriter()).initialize(
        datasource.id
    )

    assert job.status == ScanStatus.READY
    snapshot = catalog.get_active_snapshot(datasource.id)
    assert snapshot is not None
    assert snapshot.document_status == DocumentStatus.FAILED
    assert snapshot.document_path is None
    assert "OSError" in (snapshot.document_error or "")

    recovered = DatabaseInitializer(catalog).regenerate_profile_documents(datasource.id)

    assert recovered.document_status == DocumentStatus.READY
    assert recovered.document_path is not None
    assert recovered.document_error is None
    assert "画像文档生成失败，可在扫描快照上单独重试" not in recovered.warnings
    assert catalog.get_active_snapshot(datasource.id).document_status == DocumentStatus.READY


def test_ready_snapshot_switch_is_atomic(tmp_path) -> None:
    catalog = Catalog(tmp_path / "catalog.db")
    datasource = catalog.create_secure_datasource(
        SecureDatasourceCreate(
            name="sales",
            kind="relational",
            connection_profile=ConnectionProfile(
                driver="sqlite", endpoint=ConnectionEndpoint(path="sales.db")
            ),
        )
    )
    first = _snapshot(datasource, 1)
    second = _snapshot(datasource, 2)

    catalog.save_snapshot(first)
    catalog.save_snapshot(second)

    assert catalog.get_active_snapshot(datasource.id).id == second.id


def test_profile_builder_infers_nested_document_paths_from_twenty_records() -> None:
    datasource = Datasource(
        id="ds_mongo",
        name="crm",
        kind=DatasourceKind.DOCUMENT,
        workspace_id="default",
        driver="mongodb",
    )
    records = [
        {
            "_id": index,
            "customer": {"name": f"C{index}", "email": f"c{index}@example.com"},
            "tags": ["active"],
        }
        for index in range(20)
    ]
    profile, _ = ProfileBuilder().build(
        datasource,
        [DatasetInfo(datasource_id=datasource.id, name="orders", kind="collection")],
        [],
        [
            DatasetSample(
                datasource_id=datasource.id,
                dataset="orders",
                rows=records[:3],
            )
        ],
        {"orders": records},
        ScanPolicy(),
    )

    data_object = profile.namespaces[0].data_objects[0]
    fields = {field.path: field for field in data_object.fields}
    assert data_object.object_kind == DataObjectKind.COLLECTION
    assert data_object.profiled_record_count == 20
    assert len(data_object.previews) == 3
    assert fields["customer.name"].observed_count == 20
    assert fields["customer.name"].sample_values == [f"C{index}" for index in range(10)]
    assert fields["customer.email"].sample_values == ["<redacted>"]
    assert fields["tags"].array is True


def test_retrieval_and_query_preparation_use_only_retrieved_fields(tmp_path) -> None:
    catalog = Catalog(tmp_path / "catalog.db")
    datasource = catalog.create_secure_datasource(
        SecureDatasourceCreate(
            name="sales",
            kind="relational",
            connection_profile=ConnectionProfile(
                driver="sqlite", endpoint=ConnectionEndpoint(path="sales.db")
            ),
        )
    )
    catalog.save_snapshot(_snapshot(datasource, 1))

    prepared = QueryPreparationPipeline(catalog).prepare(
        "orders 的 amount 总额，前 5 条", max_rows=100
    )

    assert prepared.plan.datasource_id == datasource.id
    assert prepared.plan.data_object_ids == [f"obj_{datasource.id}"]
    assert prepared.plan.metrics == [f"obj_{datasource.id}::amount"]
    assert prepared.plan.limit == 5
    assert 'SUM(t0."amount")' in prepared.generated_query.command
    assert prepared.generated_query.command.endswith("LIMIT 5")


def test_query_preparation_uses_confirmed_relationship_inside_one_datasource(tmp_path) -> None:
    catalog = Catalog(tmp_path / "catalog.db")
    datasource = catalog.create_secure_datasource(
        SecureDatasourceCreate(
            name="sales",
            kind="relational",
            connection_profile=ConnectionProfile(
                driver="sqlite", endpoint=ConnectionEndpoint(path="sales.db")
            ),
        )
    )
    snapshot = _snapshot(datasource, 1)
    orders = snapshot.profile.namespaces[0].data_objects[0]
    orders.fields.append(FieldProfile(name="customer_id", path="customer_id", data_type="integer"))
    customers = DataObjectProfile(
        id=f"customers_{datasource.id}",
        datasource_id=datasource.id,
        name="customers",
        object_kind="table",
        fields=[
            FieldProfile(name="id", path="id", data_type="integer", primary_key=True),
            FieldProfile(name="region", path="region", data_type="text"),
        ],
    )
    snapshot.profile.namespaces[0].data_objects.append(customers)
    snapshot.relationships = [
        RelationshipProfile(
            id="orders_customer",
            from_datasource_id=datasource.id,
            from_object_id=orders.id,
            from_field_path="customer_id",
            to_datasource_id=datasource.id,
            to_object_id=customers.id,
            to_field_path="id",
            relationship_type="foreign_key",
            origin=MetadataOrigin.DATABASE,
            confirmed=True,
        )
    ]
    catalog.save_snapshot(snapshot)

    prepared = QueryPreparationPipeline(catalog).prepare(
        "按 customers region 查询 orders amount 总额", max_rows=100
    )

    assert set(prepared.plan.data_object_ids) == {orders.id, customers.id}
    assert prepared.plan.relationship_ids == ["orders_customer"]
    assert " JOIN " in prepared.generated_query.command
    assert 't0."customer_id" = t1."id"' in prepared.generated_query.command
    assert 'SELECT t1."region", SUM(t0."amount")' in prepared.generated_query.command


def test_retriever_enforces_global_field_budget() -> None:
    datasource = Datasource(
        id="ds_many",
        name="many",
        kind="relational",
        workspace_id="default",
        driver="sqlite",
    )
    objects = [
        DataObjectProfile(
            id=f"object_{index}",
            datasource_id=datasource.id,
            name=f"orders_{index}",
            object_kind="table",
            fields=[
                FieldProfile(name=f"field_{field}", path=f"field_{field}", data_type="text")
                for field in range(10)
            ],
        )
        for index in range(5)
    ]
    profile = CompanyDataProfile(
        workspace_id="default",
        data_sources=[
            DataSourceProfile(
                datasource_id=datasource.id,
                name=datasource.name,
                kind=datasource.kind,
                driver="sqlite",
                namespaces=[NamespaceProfile(name="default", data_objects=objects)],
            )
        ],
        generated_at=datetime.now(UTC),
    )
    intent = RuleBasedQueryIntentParser().parse("orders")

    context = ProfileRetriever().retrieve(profile, intent)

    assert (
        sum(
            len(item.profile.fields)
            for source in context.data_sources
            for item in source.data_objects
        )
        == 30
    )


def _snapshot(datasource: Datasource, version: int) -> ScanSnapshot:
    data_object = DataObjectProfile(
        id=f"obj_{datasource.id}",
        datasource_id=datasource.id,
        name="orders",
        object_kind="table",
        fields=[
            FieldProfile(name="id", path="id", data_type="integer", primary_key=True),
            FieldProfile(name="amount", path="amount", data_type="decimal"),
            FieldProfile(name="region", path="region", data_type="text"),
        ],
        previews=[SamplePreview(values={"id": 1, "amount": 10, "region": "east"})],
    )
    profile = DataSourceProfile(
        datasource_id=datasource.id,
        name=datasource.name,
        kind=datasource.kind,
        driver=datasource.driver or "sqlite",
        namespaces=[NamespaceProfile(name="default", data_objects=[data_object])],
    )
    return ScanSnapshot(
        id=f"snapshot_{version}",
        datasource_id=datasource.id,
        version=version,
        status=ScanStatus.READY,
        profile=profile,
        relationships=[
            RelationshipProfile(
                id=f"relationship_{version}",
                from_datasource_id=datasource.id,
                from_object_id=data_object.id,
                to_datasource_id=datasource.id,
                to_object_id=data_object.id,
                relationship_type="self_test",
                origin=MetadataOrigin.DATABASE,
            )
        ],
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        active=True,
    )
