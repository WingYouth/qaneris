from __future__ import annotations

from typing import Any

from smartdata.adapters.base import DataSourceAdapter
from smartdata.adapters.document.couchdb import CouchDBAdapter
from smartdata.adapters.document.mongodb import MongoDBAdapter
from smartdata.adapters.graph.neo4j import Neo4jAdapter
from smartdata.adapters.key_value.redis import RedisAdapter
from smartdata.adapters.relational.sqlalchemy import SQLAlchemyAdapter
from smartdata.adapters.relational.sqlite import SQLiteAdapter
from smartdata.adapters.search.engine import SearchAdapter
from smartdata.adapters.time_series.influxdb import InfluxDBAdapter
from smartdata.adapters.vector.milvus import MilvusAdapter
from smartdata.adapters.vector.qdrant import QdrantAdapter
from smartdata.adapters.vector.weaviate import WeaviateAdapter
from smartdata.adapters.wide_column.cassandra import CassandraAdapter
from smartdata.adapters.wide_column.hbase import HBaseAdapter
from smartdata.contracts import DatasourceKind

AdapterClass = type[DataSourceAdapter]

_ADAPTERS: dict[tuple[DatasourceKind, str], AdapterClass] = {}

_CONNECTION_EXAMPLES: dict[str, dict[str, Any]] = {
    "sqlite": {"path": "examples/sales_2026_07.db"},
    "postgresql": {"url": "postgresql+psycopg://readonly:password@localhost:5432/database"},
    "mysql": {"url": "mysql+pymysql://readonly:password@localhost:3306/database"},
    "oracle": {"url": "oracle+oracledb://readonly:password@localhost:1521/?service_name=service"},
    "sqlserver": {
        "url": "mssql+pyodbc://readonly:password@localhost/database"
        "?driver=ODBC+Driver+18+for+SQL+Server"
    },
    "redis": {"url": "redis://localhost:6379/0"},
    "mongodb": {"url": "mongodb://localhost:27017", "database": "database"},
    "couchdb": {
        "url": "http://localhost:5984",
        "database": "database",
        "username": "readonly",
        "password": "password",
    },
    "cassandra": {"contact_points": ["127.0.0.1"], "port": 9042, "keyspace": "keyspace"},
    "hbase": {"host": "localhost", "port": 9090},
    "neo4j": {"url": "bolt://localhost:7687", "username": "neo4j", "password": "password"},
    "influxdb": {
        "url": "http://localhost:8086",
        "token": "token",
        "org": "org",
        "bucket": "bucket",
    },
    "timescaledb": {"url": "postgresql+psycopg://readonly:password@localhost:5432/database"},
    "elasticsearch": {"url": "http://localhost:9200"},
    "opensearch": {"url": "http://localhost:9200"},
    "milvus": {"url": "http://localhost:19530", "database": "default"},
    "qdrant": {"url": "http://localhost:6333"},
    "weaviate": {"url": "http://localhost:8080"},
    "clickhouse": {"url": "clickhousedb://readonly:password@localhost:8123/database"},
    "snowflake": {"url": "snowflake://user:password@account/database/schema?warehouse=warehouse"},
    "bigquery": {"url": "bigquery://project/dataset"},
}

_QUERY_LANGUAGES = {
    "sqlite": "sql",
    "postgresql": "sql",
    "mysql": "sql",
    "oracle": "sql",
    "sqlserver": "sql",
    "timescaledb": "sql",
    "clickhouse": "sql",
    "snowflake": "sql",
    "bigquery": "sql",
    "redis": "redis_json",
    "mongodb": "mongodb_json",
    "couchdb": "couchdb_mango",
    "cassandra": "cql",
    "hbase": "hbase_json",
    "neo4j": "cypher",
    "influxdb": "flux",
    "elasticsearch": "search_json",
    "opensearch": "search_json",
    "milvus": "vector_filter_json",
    "qdrant": "vector_filter_json",
    "weaviate": "graphql",
}


def register_adapter(kind: DatasourceKind, driver: str, adapter_cls: AdapterClass) -> None:
    """Register an adapter class; a later registration replaces the existing one."""
    key = (DatasourceKind(kind), driver)
    _ADAPTERS[key] = adapter_cls


def get_adapter_class(kind: DatasourceKind, driver: str | None) -> AdapterClass:
    normalized_kind = DatasourceKind(kind)
    if driver is not None:
        adapter_cls = _ADAPTERS.get((normalized_kind, driver))
        if adapter_cls is not None:
            return adapter_cls
    raise NotImplementedError(
        f"Adapter is not implemented for kind={normalized_kind.value}, driver={driver}"
    )


def create_adapter(
    datasource_id: str, kind: DatasourceKind, connection: dict[str, Any]
) -> DataSourceAdapter:
    adapter_cls = get_adapter_class(kind, connection.get("driver"))
    return adapter_cls(datasource_id, connection)


def list_registered_adapters() -> list[dict[str, Any]]:
    return [
        {
            "kind": kind.value,
            "driver": driver,
            "query_language": _QUERY_LANGUAGES.get(driver, "native"),
            "connection_example": _CONNECTION_EXAMPLES.get(driver, {}),
        }
        for kind, driver in sorted(_ADAPTERS, key=lambda item: (item[0].value, item[1]))
    ]


register_adapter(DatasourceKind.RELATIONAL, "sqlite", SQLiteAdapter)
register_adapter(DatasourceKind.RELATIONAL, "postgresql", SQLAlchemyAdapter)
register_adapter(DatasourceKind.RELATIONAL, "mysql", SQLAlchemyAdapter)
register_adapter(DatasourceKind.RELATIONAL, "oracle", SQLAlchemyAdapter)
register_adapter(DatasourceKind.RELATIONAL, "sqlserver", SQLAlchemyAdapter)
register_adapter(DatasourceKind.KEY_VALUE, "redis", RedisAdapter)
register_adapter(DatasourceKind.DOCUMENT, "mongodb", MongoDBAdapter)
register_adapter(DatasourceKind.DOCUMENT, "couchdb", CouchDBAdapter)
register_adapter(DatasourceKind.WIDE_COLUMN, "cassandra", CassandraAdapter)
register_adapter(DatasourceKind.WIDE_COLUMN, "hbase", HBaseAdapter)
register_adapter(DatasourceKind.GRAPH, "neo4j", Neo4jAdapter)
register_adapter(DatasourceKind.TIME_SERIES, "influxdb", InfluxDBAdapter)
register_adapter(DatasourceKind.SEARCH, "elasticsearch", SearchAdapter)
register_adapter(DatasourceKind.SEARCH, "opensearch", SearchAdapter)
register_adapter(DatasourceKind.VECTOR, "milvus", MilvusAdapter)
register_adapter(DatasourceKind.VECTOR, "qdrant", QdrantAdapter)
register_adapter(DatasourceKind.VECTOR, "weaviate", WeaviateAdapter)
register_adapter(DatasourceKind.TIME_SERIES, "timescaledb", SQLAlchemyAdapter)
register_adapter(DatasourceKind.OLAP, "clickhouse", SQLAlchemyAdapter)
register_adapter(DatasourceKind.OLAP, "snowflake", SQLAlchemyAdapter)
register_adapter(DatasourceKind.OLAP, "bigquery", SQLAlchemyAdapter)
