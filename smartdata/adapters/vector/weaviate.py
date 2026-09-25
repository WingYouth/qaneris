"""Weaviate vector adapter."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import httpx

from smartdata.adapters.base import DataSourceAdapter
from smartdata.adapters.native_values import normalize_sample_row
from smartdata.contracts import DatasetInfo, DatasetSample, FieldInfo, NormalizedResult


class WeaviateAdapter(DataSourceAdapter):
    @contextmanager
    def _client(self) -> Iterator[httpx.Client]:
        url = self.connection.get("url")
        if not url:
            raise ValueError("Weaviate connection requires url")
        headers = {}
        if self.connection.get("api_key"):
            headers["Authorization"] = f"Bearer {self.connection['api_key']}"
        # This adapter stays on httpx rather than moving to the official Weaviate SDK; TLS reaches
        # it the same way CouchDB's does, through ``verify`` accepting the materializer's context.
        with httpx.Client(
            base_url=str(url).rstrip("/"),
            headers=headers,
            timeout=10,
            verify=self.connection.get("ssl_context", True),
        ) as client:
            yield client

    def test_connection(self) -> None:
        with self._client() as client:
            response = client.get("/v1/.well-known/ready")
            response.raise_for_status()

    def _schema(self) -> list[dict]:
        with self._client() as client:
            response = client.get("/v1/schema")
            response.raise_for_status()
            return response.json().get("classes", [])

    def scan_metadata(self) -> list[DatasetInfo]:
        return [
            DatasetInfo(
                datasource_id=self.datasource_id,
                name=item["class"],
                kind="collection",
                fields=[
                    FieldInfo(name=field["name"], data_type=",".join(field.get("dataType", [])))
                    for field in item.get("properties", [])
                ],
            )
            for item in self._schema()
        ]

    def _objects(self, collection: str, limit: int) -> list[dict]:
        with self._client() as client:
            response = client.get(
                "/v1/objects",
                params={"class": collection, "limit": limit, "include": "classification"},
            )
            response.raise_for_status()
            objects = response.json().get("objects", [])
        return [
            normalize_sample_row({"id": item.get("id"), **item.get("properties", {})})
            for item in objects
        ]

    def scan_samples(self, datasets: list[DatasetInfo], limit: int = 3) -> list[DatasetSample]:
        bounded = max(0, min(limit, 3))
        return [
            DatasetSample(
                datasource_id=self.datasource_id,
                dataset=dataset.name,
                rows=self._objects(dataset.name, bounded),
            )
            for dataset in datasets
        ]

    def scan_profile_records(
        self, datasets: list[DatasetInfo], limit: int = 20
    ) -> dict[str, list[dict]]:
        bounded = max(0, min(limit, 20))
        return {dataset.name: self._objects(dataset.name, bounded) for dataset in datasets}

    def execute(self, query: str, max_rows: int = 200) -> NormalizedResult:
        stripped = query.strip()
        if not stripped.startswith("{") or any(
            token in stripped.lower() for token in ("mutation", "delete", "update")
        ):
            raise ValueError("Only a read-only Weaviate GraphQL query is allowed")
        with self._client() as client:
            response = client.post("/v1/graphql", json={"query": query})
            response.raise_for_status()
            data = response.json().get("data", {})
        rows = [normalize_sample_row({"result": data})]
        return NormalizedResult(
            source=self.datasource_id,
            dataset="query",
            columns=["result"],
            rows=rows,
            row_count=1,
        )
