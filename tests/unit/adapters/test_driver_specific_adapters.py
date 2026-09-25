"""阶段二新增驱动的 per-driver 行为测试（#16）。

不依赖任何容器常驻：CouchDB 用假 httpx 客户端复刻 `_all_docs` 的分页语义，
Cassandra 用假 session / cluster metadata 复刻 keyspace 与表结构。

重点覆盖一条真实发生过的回归：**守卫用的设计文档（`_design/`）排在
`_all_docs` 首位，污染了 `scan_metadata` 的字段集合与 `scan_samples` 的样本**。
本次修复前 `tests/` 里没有任何 CouchDB 扫描用例，所以它溜过了全部测试，
这组用例就是为此补的。

本文件只验证 adapter 行为；测试数据加载器的行为由测试数据集仓库独立验证。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Self

import pytest

from qaneris.adapters.document.couchdb import CouchDBAdapter
from qaneris.adapters.document.mongodb import MongoDBAdapter
from qaneris.adapters.wide_column.cassandra import CassandraAdapter
from qaneris.contracts import DatasetInfo, FieldInfo

# ── MongoDB 测试替身 ────────────────────────────────────────────────────────


class _FakeMongoCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def limit(self, value: int) -> _FakeMongoCursor:
        return _FakeMongoCursor(self.rows[:value])

    def __iter__(self):
        return iter(self.rows)


class _FakeMongoCollection:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def find(self) -> _FakeMongoCursor:
        return _FakeMongoCursor(self.rows)


class _FakeMongoDatabase:
    def __init__(self, collections: dict[str, list[dict[str, Any]]]) -> None:
        self.collections = collections

    def list_collection_names(self) -> list[str]:
        return list(self.collections)

    def __getitem__(self, name: str) -> _FakeMongoCollection:
        return _FakeMongoCollection(self.collections[name])


class _FakeMongoClient:
    def __init__(self, collections: dict[str, list[dict[str, Any]]]) -> None:
        self.database = _FakeMongoDatabase(collections)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def __getitem__(self, name: str) -> _FakeMongoDatabase:
        return self.database


class _FakeMongoAdapter(MongoDBAdapter):
    def __init__(self, collections: dict[str, list[dict[str, Any]]]) -> None:
        super().__init__("ds_mongo", {"database": "retail", "driver": "mongodb"})
        self.fake = _FakeMongoClient(collections)

    def _client(self):  # type: ignore[override]
        return self.fake


def test_mongodb_scan_metadata_infers_schema_without_persisting_values() -> None:
    adapter = _FakeMongoAdapter(
        {
            "orders": [
                {"_id": "order-1", "status": "completed", "amount": 12.5},
                {"_id": "order-2", "status": "pending", "amount": 9.0},
            ]
        }
    )

    datasets = adapter.scan_metadata()

    assert [dataset.name for dataset in datasets] == ["orders"]
    assert {field.name for field in datasets[0].fields} == {"_id", "amount", "status"}
    assert next(field for field in datasets[0].fields if field.name == "_id").primary_key
    assert "completed" not in {field.name for field in datasets[0].fields}

# ── CouchDB 测试替身 ─────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeCouchClient:
    """只实现 adapter 用到的两个端点，并复刻 `_all_docs` 的 startkey/skip/limit。"""

    def __init__(
        self,
        rows: list[dict[str, Any]] | None = None,
        docs: list[dict[str, Any]] | None = None,
    ) -> None:
        # rows 需已按 _id 排好序，与 CouchDB 返回顺序一致
        self.rows = list(rows or [])
        self.docs = list(docs or [])
        self.get_params: list[dict[str, Any]] = []
        self.post_payloads: list[dict[str, Any]] = []

    def get(self, url: str, params: dict[str, Any] | None = None) -> _FakeResponse:
        params = dict(params or {})
        self.get_params.append(params)
        rows = self.rows
        if "startkey" in params:
            start = json.loads(params["startkey"])
            rows = [row for row in rows if row["id"] >= start]
            if params.get("skip"):
                rows = rows[int(params["skip"]) :]
        if params.get("limit") is not None:
            rows = rows[: int(params["limit"])]
        return _FakeResponse(
            {"rows": [{"id": row["id"], "doc": row["doc"]} for row in rows]}
        )

    def post(self, url: str, json: dict[str, Any] | None = None) -> _FakeResponse:
        payload = dict(json or {})
        self.post_payloads.append(payload)
        limit = int(payload.get("limit", len(self.docs)))
        return _FakeResponse({"docs": self.docs[:limit]})


class _FakeCouchAdapter(CouchDBAdapter):
    """把 `_client` 替换为假客户端，避免任何真实网络请求。"""

    def __init__(self, connection: dict[str, Any], client: _FakeCouchClient) -> None:
        super().__init__("ds_couch", connection)
        self.fake = client

    @contextmanager
    def _client(self):  # type: ignore[override]
        yield self.fake


def _couch(
    rows: list[dict[str, Any]] | None = None, docs: list[dict[str, Any]] | None = None
) -> tuple[_FakeCouchAdapter, _FakeCouchClient]:
    client = _FakeCouchClient(rows=rows, docs=docs)
    connection = {"url": "http://couch.example:5984", "database": "retail", "driver": "couchdb"}
    return _FakeCouchAdapter(connection, client), client


_DESIGN_DOC = {
    "_id": "_design/readonly-guard",
    "language": "javascript",
    "validate_doc_update": "function(newDoc, oldDoc, userCtx) { }",
}


def _design_row() -> dict[str, Any]:
    return {"id": _DESIGN_DOC["_id"], "doc": dict(_DESIGN_DOC)}


# ── CouchDB：连接字段校验 ────────────────────────────────────────────────────


def test_couchdb_requires_database_and_url() -> None:
    with pytest.raises(ValueError, match="requires database"):
        CouchDBAdapter("ds", {"url": "http://couch.example:5984"}).scan_metadata()
    with pytest.raises(ValueError, match="requires url"):
        CouchDBAdapter("ds", {"database": "retail"}).scan_metadata()


# ── CouchDB：设计文档必须被排除（本次回归的守门用例）─────────────────────────


def test_couchdb_documents_exclude_design_documents() -> None:
    rows = [
        _design_row(),
        {"id": "order_1", "doc": {"_id": "order_1", "type": "order"}},
        {"id": "order_10", "doc": {"_id": "order_10", "type": "order"}},
    ]
    adapter, _ = _couch(rows=rows)

    assert [doc["_id"] for doc in adapter._documents(2)] == ["order_1", "order_10"]


def test_couchdb_documents_top_up_when_design_documents_fill_first_page() -> None:
    rows = [{"id": f"_design/d{i}", "doc": {"_id": f"_design/d{i}"}} for i in range(3)]
    rows += [{"id": f"order_{i}", "doc": {"_id": f"order_{i}"}} for i in range(1, 5)]
    adapter, client = _couch(rows=rows)

    assert [doc["_id"] for doc in adapter._documents(2)] == ["order_1", "order_2"]
    # 确实翻了页，而不是少采一个文档
    assert len(client.get_params) >= 2


def test_couchdb_documents_keep_ids_that_sort_before_design_documents() -> None:
    """数字/符号开头的合法文档排在 `_design` 之前，不能被范围跳过误伤。

    实测 ICU 排序下 ``startkey="_design0"`` 会连带跳过这些文档，
    所以实现必须走客户端前缀过滤。
    """
    rows = [
        {"id": "0digit", "doc": {"_id": "0digit"}},
        {"id": "5digit", "doc": {"_id": "5digit"}},
        _design_row(),
        {"id": "alpha", "doc": {"_id": "alpha"}},
    ]
    adapter, _ = _couch(rows=rows)

    assert [doc["_id"] for doc in adapter._documents(3)] == ["0digit", "5digit", "alpha"]


def test_couchdb_documents_zero_limit_returns_empty() -> None:
    adapter, client = _couch(rows=[{"id": "order_1", "doc": {"_id": "order_1"}}])

    assert adapter._documents(0) == []
    assert client.get_params == []


def test_couchdb_scan_metadata_excludes_design_document_fields() -> None:
    rows = [
        _design_row(),
        {
            "id": "order_1",
            "doc": {"_id": "order_1", "type": "order", "status": "已完成"},
        },
    ]
    adapter, _ = _couch(rows=rows)

    datasets = adapter.scan_metadata()

    assert [dataset.name for dataset in datasets] == ["retail"]
    assert datasets[0].kind == "document_database"
    field_names = {field.name for field in datasets[0].fields}
    assert "validate_doc_update" not in field_names
    assert "language" not in field_names
    assert {"_id", "type", "status"} <= field_names


def test_couchdb_scan_samples_exclude_design_documents_and_redact() -> None:
    rows = [
        _design_row(),
        {"id": "user_1", "doc": {"_id": "user_1", "mobile": "13800000000", "name": "张三"}},
    ]
    adapter, _ = _couch(rows=rows)
    datasets = adapter.scan_metadata()

    samples = adapter.scan_samples(datasets, limit=3)

    assert [row["_id"] for row in samples[0].rows] == ["user_1"]
    assert samples[0].rows[0]["mobile"] == "<redacted>"
    assert samples[0].rows[0]["name"] == "张三"


def test_couchdb_scan_samples_bounds_limit_to_three() -> None:
    rows = [{"id": f"order_{i}", "doc": {"_id": f"order_{i}"}} for i in range(10)]
    adapter, _ = _couch(rows=rows)
    datasets = adapter.scan_metadata()

    samples = adapter.scan_samples(datasets, limit=99)

    assert len(samples[0].rows) == 3


# ── CouchDB：Mango selector 构造与执行 ───────────────────────────────────────


def test_couchdb_execute_requires_selector() -> None:
    adapter, _ = _couch()

    with pytest.raises(TypeError, match="requires selector"):
        adapter.execute(json.dumps({"limit": 5}))


def test_couchdb_execute_requires_json_payload() -> None:
    adapter, _ = _couch()

    with pytest.raises(ValueError, match="JSON object"):
        adapter.execute("SELECT * FROM retail")
    with pytest.raises(TypeError, match="JSON object"):
        adapter.execute(json.dumps(["not", "an", "object"]))


def test_couchdb_execute_passes_selector_through_untouched() -> None:
    adapter, client = _couch(docs=[{"_id": "order_1", "status": "已完成"}])
    selector = {"type": "order", "status": "已完成"}

    adapter.execute(json.dumps({"selector": selector, "fields": ["_id"], "limit": 5}))

    assert client.post_payloads[0]["selector"] == selector
    assert client.post_payloads[0]["fields"] == ["_id"]


def test_couchdb_execute_requests_one_extra_row_to_detect_truncation() -> None:
    docs = [{"_id": f"order_{i}", "status": "已完成"} for i in range(10)]
    adapter, client = _couch(docs=docs)

    result = adapter.execute(
        json.dumps({"selector": {"status": "已完成"}, "limit": 3}), max_rows=200
    )

    assert client.post_payloads[0]["limit"] == 4  # limit + 1
    assert result.row_count == 3
    assert result.truncated is True


def test_couchdb_execute_clamps_limit_to_max_rows() -> None:
    docs = [{"_id": f"order_{i}"} for i in range(50)]
    adapter, client = _couch(docs=docs)

    result = adapter.execute(json.dumps({"selector": {}, "limit": 1000}), max_rows=5)

    assert client.post_payloads[0]["limit"] == 6
    assert result.row_count == 5
    assert result.truncated is True


def test_couchdb_execute_redacts_sensitive_fields() -> None:
    adapter, _ = _couch(
        docs=[{"_id": "user_1", "email": "a@b.c", "api_key": "k", "name": "张三"}]
    )

    row = adapter.execute(json.dumps({"selector": {"type": "user_profile"}})).rows[0]

    assert row["email"] == "<redacted>"
    assert row["api_key"] == "<redacted>"
    assert row["name"] == "张三"


# ── Cassandra 测试替身 ───────────────────────────────────────────────────────


class _FakeCqlRow:
    def __init__(self, **values: Any) -> None:
        self._values = values

    def _asdict(self) -> dict[str, Any]:
        return dict(self._values)


class _FakeColumn:
    def __init__(self, name: str, cql_type: str) -> None:
        self.name = name
        self.cql_type = cql_type


class _FakeTable:
    def __init__(
        self,
        columns: list[_FakeColumn],
        partition_key: list[_FakeColumn],
        clustering_key: list[_FakeColumn] | None = None,
    ) -> None:
        self.columns = {column.name: column for column in columns}
        self.partition_key = tuple(partition_key)
        self.clustering_key = tuple(clustering_key or [])


class _FakeKeyspace:
    def __init__(self, tables: dict[str, _FakeTable]) -> None:
        self.tables = tables


class _FakeClusterMetadata:
    def __init__(self, keyspaces: dict[str, _FakeKeyspace]) -> None:
        self.keyspaces = keyspaces


class _FakeCluster:
    def __init__(self, keyspaces: dict[str, _FakeKeyspace]) -> None:
        self.metadata = _FakeClusterMetadata(keyspaces)


class _FakeCassandraSession:
    def __init__(self, cluster: Any = None, rows: list[Any] | None = None) -> None:
        self.cluster = cluster
        self.rows = list(rows or [])
        self.queries: list[str] = []

    def execute(self, query: Any) -> list[Any]:
        self.queries.append(str(query))
        return list(self.rows)

    def shutdown(self) -> None:
        return None


class _FakeCassandraAdapter(CassandraAdapter):
    def __init__(self, connection: dict[str, Any], session: _FakeCassandraSession) -> None:
        super().__init__("ds_cassandra", connection)
        self.fake = session

    @contextmanager
    def _session(self):  # type: ignore[override]
        yield self.fake


def _behavior_events_table() -> _FakeTable:
    customer_id = _FakeColumn("customer_id", "int")
    event_time = _FakeColumn("event_time", "timestamp")
    event_type = _FakeColumn("event_type", "text")
    return _FakeTable(
        columns=[customer_id, event_time, event_type,
                 _FakeColumn("sku_id", "int"), _FakeColumn("source", "text")],
        partition_key=[customer_id],
        clustering_key=[event_time, event_type],
    )


def _cassandra(
    session: _FakeCassandraSession | None = None, keyspace: str = "retail"
) -> _FakeCassandraAdapter:
    return _FakeCassandraAdapter({"keyspace": keyspace, "driver": "cassandra"},
                                 session or _FakeCassandraSession())


# ── Cassandra：连接字段与只读校验 ────────────────────────────────────────────


def test_cassandra_requires_keyspace() -> None:
    with pytest.raises(ValueError, match="requires keyspace"):
        CassandraAdapter("ds", {"contact_points": ["127.0.0.1"]})._keyspace()


@pytest.mark.parametrize(
    "query",
    [
        "INSERT INTO retail.behavior_events (customer_id) VALUES (1)",
        "DROP TABLE retail.behavior_events",
        "UPDATE retail.behavior_events SET source = 'x'",
        "DELETE FROM retail.behavior_events",
        "TRUNCATE retail.behavior_events",
    ],
)
def test_cassandra_execute_rejects_non_select(query: str) -> None:
    # 校验发生在建立 session 之前，因此这里无需任何替身
    with pytest.raises(ValueError, match="Only one Cassandra SELECT statement"):
        CassandraAdapter("ds", {"keyspace": "retail"}).execute(query)


def test_cassandra_execute_rejects_multiple_statements() -> None:
    with pytest.raises(ValueError, match="Only one Cassandra SELECT statement"):
        CassandraAdapter("ds", {"keyspace": "retail"}).execute(
            "SELECT * FROM a; SELECT * FROM b"
        )


def test_cassandra_execute_allows_leading_whitespace_and_trailing_semicolon() -> None:
    session = _FakeCassandraSession(rows=[_FakeCqlRow(customer_id=1, event_type="加购")])
    adapter = _cassandra(session)

    result = adapter.execute("  SELECT customer_id, event_type FROM behavior_events;")

    assert session.queries == ["  SELECT customer_id, event_type FROM behavior_events;"]
    assert result.columns == ["customer_id", "event_type"]
    assert result.row_count == 1
    assert result.truncated is False


def test_cassandra_execute_flags_truncation() -> None:
    session = _FakeCassandraSession(rows=[_FakeCqlRow(n=index) for index in range(5)])
    adapter = _cassandra(session)

    result = adapter.execute("SELECT n FROM t", max_rows=2)

    assert result.row_count == 2
    assert result.truncated is True


# ── Cassandra：keyspace / 表扫描 ─────────────────────────────────────────────


def test_cassandra_scan_metadata_marks_partition_and_clustering_keys() -> None:
    cluster = _FakeCluster({"retail": _FakeKeyspace({"behavior_events": _behavior_events_table()})})
    adapter = _cassandra(_FakeCassandraSession(cluster=cluster))

    datasets = adapter.scan_metadata()

    assert [dataset.name for dataset in datasets] == ["behavior_events"]
    assert datasets[0].kind == "table"
    assert datasets[0].datasource_id == "ds_cassandra"
    fields = {field.name: field for field in datasets[0].fields}
    assert fields["customer_id"].primary_key is True
    assert fields["event_time"].primary_key is True
    assert fields["event_type"].primary_key is True
    assert fields["sku_id"].primary_key is False
    assert fields["source"].primary_key is False
    assert fields["customer_id"].data_type == "int"
    assert fields["event_time"].data_type == "timestamp"


def test_cassandra_scan_metadata_sorts_tables_by_name() -> None:
    tables = {"zebra": _behavior_events_table(), "alpha": _behavior_events_table()}
    adapter = _cassandra(_FakeCassandraSession(cluster=_FakeCluster({"retail": _FakeKeyspace(tables)})))

    assert [dataset.name for dataset in adapter.scan_metadata()] == ["alpha", "zebra"]


def test_cassandra_scan_samples_quotes_table_and_bounds_limit_to_three() -> None:
    session = _FakeCassandraSession(rows=[_FakeCqlRow(customer_id=1)])
    adapter = _cassandra(session)
    datasets = [DatasetInfo(datasource_id="ds", name='odd"name', kind="table", fields=[])]

    samples = adapter.scan_samples(datasets, limit=99)

    assert samples[0].dataset == 'odd"name'
    assert session.queries == ['SELECT * FROM "odd""name" LIMIT 3']


def test_cassandra_scan_samples_respects_zero_limit() -> None:
    session = _FakeCassandraSession()
    adapter = _cassandra(session)
    datasets = [DatasetInfo(datasource_id="ds", name="t", kind="table", fields=[])]

    adapter.scan_samples(datasets, limit=0)

    assert session.queries == ['SELECT * FROM "t" LIMIT 0']


def test_cassandra_scan_profile_records_bounds_limit_to_twenty() -> None:
    session = _FakeCassandraSession(rows=[_FakeCqlRow(customer_id=1)])
    adapter = _cassandra(session)
    datasets = [DatasetInfo(datasource_id="ds", name="t", kind="table", fields=[])]

    records = adapter.scan_profile_records(datasets, limit=999)

    assert session.queries == ['SELECT * FROM "t" LIMIT 20']
    assert list(records) == ["t"]


def test_cassandra_field_info_model_is_used() -> None:
    """守住 scan_metadata 返回的是 FieldInfo 模型而不是裸 dict。"""
    cluster = _FakeCluster({"retail": _FakeKeyspace({"behavior_events": _behavior_events_table()})})
    adapter = _cassandra(_FakeCassandraSession(cluster=cluster))

    assert all(
        isinstance(field, FieldInfo) for field in adapter.scan_metadata()[0].fields
    )


# ── Cassandra：_session 的鉴权与连接参数装配 ─────────────────────────────────


def test_cassandra_session_builds_cluster_with_plain_text_auth(monkeypatch) -> None:
    """只读账号必须把 username/password 传成 PlainTextAuthProvider。"""
    cassandra_cluster = pytest.importorskip("cassandra.cluster")
    cassandra_auth = pytest.importorskip("cassandra.auth")

    captured: dict[str, Any] = {}

    class RecordingCluster:
        def __init__(self, contact_points=None, port=None, auth_provider=None) -> None:
            captured["contact_points"] = contact_points
            captured["port"] = port
            captured["auth_provider"] = auth_provider

        def connect(self, keyspace=None):
            captured["keyspace"] = keyspace
            return _FakeCassandraSession()

        def shutdown(self):
            captured["cluster_shutdown"] = True

    monkeypatch.setattr(cassandra_cluster, "Cluster", RecordingCluster)

    adapter = CassandraAdapter(
        "ds",
        {
            "keyspace": "retail",
            "contact_points": ["10.0.0.9"],
            "port": 9043,
            "username": "readonly",
            "password": "readonly",
        },
    )
    adapter.test_connection()

    assert captured["contact_points"] == ["10.0.0.9"]
    assert captured["port"] == 9043
    assert captured["keyspace"] == "retail"
    assert captured["cluster_shutdown"] is True
    provider = captured["auth_provider"]
    assert isinstance(provider, cassandra_auth.PlainTextAuthProvider)
    assert provider.username == "readonly"
    assert provider.password == "readonly"


def test_cassandra_session_without_username_has_no_auth_provider(monkeypatch) -> None:
    cassandra_cluster = pytest.importorskip("cassandra.cluster")

    captured: dict[str, Any] = {}

    class RecordingCluster:
        def __init__(self, contact_points=None, port=None, auth_provider=None) -> None:
            captured["auth_provider"] = auth_provider

        def connect(self, keyspace=None):
            return _FakeCassandraSession()

        def shutdown(self):
            return None

    monkeypatch.setattr(cassandra_cluster, "Cluster", RecordingCluster)

    CassandraAdapter("ds", {"keyspace": "retail"}).test_connection()

    assert captured["auth_provider"] is None
