"""MongoDB document adapter."""

from __future__ import annotations

from collections.abc import Iterator
from itertools import islice
from typing import Any

from qaneris.adapters.base import DataSourceAdapter
from qaneris.adapters.native_query import bounded_limit, parse_query_payload
from qaneris.adapters.native_values import normalize_sample_row
from qaneris.contracts import DatasetInfo, DatasetSample, FieldInfo, NormalizedResult
from qaneris.querying.validation.read_only import UnsafeQueryError

# 只读聚合允许的管线里，这些阶段必须拒绝：
# - $out / $merge 会写库（$merge 可写回业务集合），不是只读操作；
# - $where / $function / $accumulator 会在服务端执行 JS。
FORBIDDEN_PIPELINE_KEYS = frozenset(
    {"$out", "$merge", "$where", "$function", "$accumulator"}
)


def _collect_keys(value: Any) -> Iterator[str]:
    """递归收集管道里出现的所有键名（含嵌套的 match/group 表达式）。"""
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _collect_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _collect_keys(item)


def assert_read_only_pipeline(pipeline: list[Any]) -> None:
    forbidden = sorted(set(_collect_keys(pipeline)) & FORBIDDEN_PIPELINE_KEYS)
    if forbidden:
        raise UnsafeQueryError(
            f"MongoDB pipeline is not read-only: {', '.join(forbidden)}"
        )


class MongoDBAdapter(DataSourceAdapter):
    def _database_name(self) -> str:
        database = self.connection.get("database")
        if not database:
            raise ValueError("MongoDB connection requires database")
        return str(database)

    def _client(self) -> Any:
        try:
            from pymongo import MongoClient
        except ImportError as error:
            raise RuntimeError("MongoDB support requires qaneris-platform[mongodb]") from error
        # pymongo 只要收到 tls* 参数、而 tls 未启用，就直接抛 ConfigurationError：
        # "TLS has not been enabled but the following tls parameters have been set"。
        # 所以这些参数必须【仅在启用 TLS 时】才传，否则 registry 的官方模板
        # （{"url": "mongodb://...", "database": ...}，不含 tls）永远连不上。
        #
        # 启用 TLS 时，全部 tls* 参数（含 tlsCAFile 与 tlsCertificateKeyFile）已由
        # TLSMaterializer 生成；Adapter 只做转发，不再自己从 profile 解析证书，避免出现
        # 第二套互相冲突的 TLS 配置。
        options: dict[str, Any] = {}
        if self.connection.get("tls"):
            options["tls"] = True
            for name in (
                "tlsAllowInvalidCertificates",
                "tlsAllowInvalidHostnames",
                "tlsCAFile",
                "tlsCertificateKeyFile",
            ):
                if self.connection.get(name) is not None:
                    options[name] = self.connection[name]
            # A materializer-produced connection already carries this flag; a legacy raw
            # connection dict does not, so it is derived from ``verify_server`` as before.
            if "tlsAllowInvalidCertificates" not in options:
                options["tlsAllowInvalidCertificates"] = not bool(
                    self.connection.get("verify_server", True)
                )
        return MongoClient(
            self.connection.get("url", "mongodb://localhost:27017"),
            username=self.connection.get("username"),
            password=self.connection.get("password"),
            **options,
        )

    def test_connection(self) -> None:
        with self._client() as client:
            client.admin.command("ping")

    @staticmethod
    def _fields(rows: list[dict[str, Any]]) -> list[FieldInfo]:
        types: dict[str, str] = {}
        for row in rows:
            for key, value in row.items():
                types.setdefault(str(key), type(value).__name__)
        return [
            FieldInfo(name=name, data_type=data_type, primary_key=name == "_id")
            for name, data_type in sorted(types.items())
        ]

    def scan_metadata(self) -> list[DatasetInfo]:
        with self._client() as client:
            database = client[self._database_name()]
            datasets = []
            for name in sorted(database.list_collection_names()):
                # MongoDB has no fixed table schema. Infer field names and coarse
                # types from a bounded sample so the ScanRun catalog and Neo4j
                # publication receive the same schema evidence as the profile.
                rows = list(database[name].find().limit(3))
                datasets.append(
                    DatasetInfo(
                        datasource_id=self.datasource_id,
                        name=name,
                        kind="collection",
                        fields=self._fields(rows),
                    )
                )
            return datasets

    def scan_profile_records(
        self, datasets: list[DatasetInfo], limit: int = 20
    ) -> dict[str, list[dict[str, Any]]]:
        bounded = max(0, min(limit, 20))
        with self._client() as client:
            database = client[self._database_name()]
            return {
                dataset.name: [
                    normalize_sample_row(row)
                    for row in database[dataset.name].find().limit(bounded)
                ]
                for dataset in datasets
            }

    def scan_samples(self, datasets: list[DatasetInfo], limit: int = 3) -> list[DatasetSample]:
        bounded = max(0, min(limit, 3))
        with self._client() as client:
            database = client[self._database_name()]
            return [
                DatasetSample(
                    datasource_id=self.datasource_id,
                    dataset=dataset.name,
                    rows=[
                        normalize_sample_row(row)
                        for row in database[dataset.name].find().limit(bounded)
                    ],
                )
                for dataset in datasets
            ]

    def execute(self, query: str, max_rows: int = 200) -> NormalizedResult:
        payload = parse_query_payload(query)
        collection = payload.get("collection")
        if not isinstance(collection, str) or not collection:
            raise ValueError("MongoDB query requires collection")
        limit = bounded_limit(payload.get("limit"), max_rows)
        pipeline = payload.get("pipeline")
        if pipeline is not None:
            # 只读聚合：契约里的分库物理查询（$match/$count/$group/$unwind）走这条路径。
            # 缺了这条分支，find 会【静默忽略 pipeline】并返回全量文档，
            # 「已完成订单有多少笔」会得到一个看起来正常、其实错误的答案。
            if not isinstance(pipeline, list):
                raise TypeError("MongoDB pipeline must be a list")
            assert_read_only_pipeline(pipeline)
            with self._client() as client:
                cursor = client[self._database_name()][collection].aggregate(pipeline)
                # 用 islice 兜住行数上限，防止没有 $limit 的管道把结果整批拉回来
                rows = [
                    normalize_sample_row(row) for row in islice(cursor, limit + 1)
                ]
        else:
            filter_value = payload.get("filter") or {}
            projection = payload.get("projection")
            if (
                not isinstance(filter_value, dict)
                or projection is not None
                and not isinstance(projection, dict)
            ):
                raise ValueError("MongoDB filter and projection must be objects")
            with self._client() as client:
                cursor = client[self._database_name()][collection].find(
                    filter_value, projection
                )
                if isinstance(payload.get("sort"), list):
                    cursor = cursor.sort(payload["sort"])
                rows = [
                    normalize_sample_row(row) for row in cursor.limit(limit + 1)
                ]
        truncated = len(rows) > limit
        rows = rows[:limit]
        columns = sorted({key for row in rows for key in row})
        return NormalizedResult(
            source=self.datasource_id,
            dataset=collection,
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
        )

    def execute_native(
        self, command: str | dict[str, Any], parameters: tuple[Any, ...] = (),
        max_rows: int = 200,
    ) -> NormalizedResult:
        if not isinstance(command, dict) or parameters or max_rows < 1:
            raise ValueError("MongoDB formal execution requires a typed bounded command")
        operation = command.get("operation")
        collection = command.get("collection")
        if operation not in {"find", "aggregate"} or not isinstance(collection, str) or not collection:
            raise ValueError("MongoDB formal operation is not allowed")
        if set(_collect_keys(command)) & FORBIDDEN_PIPELINE_KEYS:
            raise UnsafeQueryError("MongoDB command contains a forbidden operator")
        pipeline = command.get("pipeline") if operation == "aggregate" else None
        if operation == "aggregate" and (
            not isinstance(pipeline, list) or not pipeline or
            not isinstance(pipeline[-1], dict)
        ):
            raise ValueError("MongoDB formal pipeline is invalid")
        limit = command.get("limit") if operation == "find" else pipeline[-1].get("$limit")
        if not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("MongoDB formal limit is invalid")
        bounded = min(limit, max_rows)
        with self._client() as client:
            source = client[self._database_name()][collection]
            if operation == "find":
                if set(command) != {"operation", "collection", "filter", "projection", "sort", "limit"}:
                    raise ValueError("MongoDB find shape is invalid")
                cursor = source.find(command["filter"], command["projection"])
                if command["sort"]:
                    cursor = cursor.sort(command["sort"])
                rows = [normalize_sample_row(row) for row in cursor.limit(bounded + 1)]
            else:
                if not isinstance(pipeline, list) or not pipeline or any(
                    not isinstance(stage, dict) or len(stage) != 1 or
                    next(iter(stage)) not in {"$match", "$group", "$project", "$sort", "$limit", "$count"}
                    for stage in pipeline
                ):
                    raise UnsafeQueryError("MongoDB pipeline stage is not allowed")
                assert_read_only_pipeline(pipeline)
                rows = [normalize_sample_row(row) for row in islice(source.aggregate(pipeline), bounded + 1)]
        truncated = len(rows) > bounded
        rows = rows[:bounded]
        return NormalizedResult(source=self.datasource_id, dataset=collection,
                                columns=sorted({key for row in rows for key in row}),
                                rows=rows, row_count=len(rows), truncated=truncated)
