"""CouchDB document adapter."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx

from smartdata.adapters.base import DataSourceAdapter
from smartdata.adapters.native_query import bounded_limit, parse_query_payload
from smartdata.adapters.native_values import normalize_sample_row
from smartdata.contracts import DatasetInfo, DatasetSample, FieldInfo, NormalizedResult


class CouchDBAdapter(DataSourceAdapter):
    def _database(self) -> str:
        database = self.connection.get("database")
        if not database:
            raise ValueError("CouchDB connection requires database")
        return str(database)

    @contextmanager
    def _client(self) -> Iterator[httpx.Client]:
        url = self.connection.get("url")
        if not url:
            raise ValueError("CouchDB connection requires url")
        auth = None
        if self.connection.get("username"):
            auth = (str(self.connection["username"]), str(self.connection.get("password", "")))
        # ``verify`` accepts the materializer's SSLContext, which already carries the CA, the client
        # pair and the verification policy. It defaults to True so a non-TLS connection behaves
        # exactly as before.
        with httpx.Client(
            base_url=str(url).rstrip("/"),
            auth=auth,
            timeout=10,
            verify=self.connection.get("ssl_context", True),
        ) as client:
            yield client

    def test_connection(self) -> None:
        with self._client() as client:
            response = client.get(f"/{self._database()}")
            response.raise_for_status()

    @staticmethod
    def _is_design_document(doc_id: str) -> bool:
        """设计文档（`_design/`）是视图与校验函数，不是业务数据。

        它们按 `_id` 排在 `_all_docs` 最前，若不排除会污染 `scan_metadata`
        的字段集合与采样内容（会看到 `validate_doc_update` 之类的字段）。
        """
        return doc_id.startswith("_design/")

    def _documents(self, limit: int) -> list[dict[str, Any]]:
        """按 `_id` 顺序取前 `limit` 个业务文档，跳过设计文档。

        不能用 `startkey="_design0"` 之类按范围跳过的写法：实测 ICU 排序下
        `-`、`.`、数字、大写字母开头的合法文档 id 都排在 `_design` 之前，
        范围跳过会静默丢掉这些文档。只能客户端过滤，并翻页补足数量，
        否则有设计文档时采样会少一个文档。
        """
        if limit <= 0:
            return []
        documents: list[dict[str, Any]] = []
        start_key: str | None = None
        with self._client() as client:
            while len(documents) < limit:
                wanted = limit - len(documents)
                params: dict[str, Any] = {
                    "include_docs": "true",
                    # 多取一行以抵消可能被跳过的设计文档
                    "limit": wanted + 1,
                }
                if start_key is not None:
                    params["startkey"] = json.dumps(start_key)
                    params["skip"] = 1
                response = client.get(f"/{self._database()}/_all_docs", params=params)
                response.raise_for_status()
                rows = response.json().get("rows", [])
                if not rows:
                    break
                for item in rows:
                    doc_id = item.get("id")
                    if doc_id:
                        start_key = doc_id
                        if self._is_design_document(doc_id):
                            continue
                    document = item.get("doc")
                    if document:
                        documents.append(document)
                    if len(documents) >= limit:
                        break
        return documents[:limit]

    def scan_metadata(self) -> list[DatasetInfo]:
        documents = self._documents(3)
        fields = sorted({str(key) for document in documents for key in document})
        return [
            DatasetInfo(
                datasource_id=self.datasource_id,
                name=self._database(),
                kind="document_database",
                fields=[FieldInfo(name=name, data_type="document_field") for name in fields],
            )
        ]

    def scan_samples(self, datasets: list[DatasetInfo], limit: int = 3) -> list[DatasetSample]:
        return [
            DatasetSample(
                datasource_id=self.datasource_id,
                dataset=self._database(),
                rows=[normalize_sample_row(row) for row in self._documents(max(0, min(limit, 3)))],
            )
        ]

    def scan_profile_records(
        self, datasets: list[DatasetInfo], limit: int = 20
    ) -> dict[str, list[dict[str, Any]]]:
        bounded = max(0, min(limit, 20))
        return {self._database(): [normalize_sample_row(row) for row in self._documents(bounded)]}

    def execute(self, query: str, max_rows: int = 200) -> NormalizedResult:
        payload = parse_query_payload(query)
        if not isinstance(payload.get("selector"), dict):
            raise TypeError("CouchDB Mango query requires selector")
        limit = bounded_limit(payload.get("limit"), max_rows)
        payload["limit"] = limit + 1
        with self._client() as client:
            response = client.post(f"/{self._database()}/_find", json=payload)
            response.raise_for_status()
            rows = [normalize_sample_row(row) for row in response.json().get("docs", [])]
        truncated = len(rows) > limit
        rows = rows[:limit]
        columns = sorted({key for row in rows for key in row})
        return NormalizedResult(
            source=self.datasource_id,
            dataset=self._database(),
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
        )
