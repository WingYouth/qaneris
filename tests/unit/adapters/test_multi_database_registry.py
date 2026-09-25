from qaneris.adapters.registry import create_adapter, get_adapter_class, list_registered_adapters
from qaneris.contracts import DatasourceKind

EXPECTED_ADAPTERS = {
    (DatasourceKind.RELATIONAL, "sqlite"),
    (DatasourceKind.RELATIONAL, "postgresql"),
    (DatasourceKind.RELATIONAL, "mysql"),
    (DatasourceKind.RELATIONAL, "oracle"),
    (DatasourceKind.RELATIONAL, "sqlserver"),
    (DatasourceKind.KEY_VALUE, "redis"),
    (DatasourceKind.DOCUMENT, "mongodb"),
    (DatasourceKind.DOCUMENT, "couchdb"),
    (DatasourceKind.WIDE_COLUMN, "cassandra"),
    (DatasourceKind.WIDE_COLUMN, "hbase"),
    (DatasourceKind.GRAPH, "neo4j"),
    (DatasourceKind.TIME_SERIES, "influxdb"),
    (DatasourceKind.TIME_SERIES, "timescaledb"),
    (DatasourceKind.SEARCH, "elasticsearch"),
    (DatasourceKind.SEARCH, "opensearch"),
    (DatasourceKind.VECTOR, "milvus"),
    (DatasourceKind.VECTOR, "qdrant"),
    (DatasourceKind.VECTOR, "weaviate"),
    (DatasourceKind.OLAP, "clickhouse"),
    (DatasourceKind.OLAP, "snowflake"),
    (DatasourceKind.OLAP, "bigquery"),
}


def test_all_mvp_database_adapters_are_registered() -> None:
    registered = {
        (DatasourceKind(item["kind"]), item["driver"]) for item in list_registered_adapters()
    }
    assert EXPECTED_ADAPTERS <= registered
    for kind, driver in EXPECTED_ADAPTERS:
        assert get_adapter_class(kind, driver) is not None
        assert create_adapter("ds_contract", kind, {"driver": driver}) is not None
