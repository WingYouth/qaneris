"""HBase wide-column adapter."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from qaneris.adapters.base import DataSourceAdapter
from qaneris.adapters.native_query import bounded_limit, parse_query_payload
from qaneris.adapters.native_values import normalize_sample_value
from qaneris.contracts import DatasetInfo, DatasetSample, FieldInfo, NormalizedResult


class HBaseAdapter(DataSourceAdapter):
    @contextmanager
    def _connection(self) -> Iterator[Any]:
        try:
            import happybase
        except ImportError as error:
            raise RuntimeError("HBase support requires qaneris-platform[hbase]") from error
        connection = happybase.Connection(
            host=self.connection.get("host", "localhost"),
            port=int(self.connection.get("port", 9090)),
            timeout=int(self.connection.get("timeout_ms", 10_000)),
        )
        connection.open()
        try:
            yield connection
        finally:
            connection.close()

    def test_connection(self) -> None:
        with self._connection() as connection:
            connection.tables()

    @staticmethod
    def _decode(value: Any) -> str:
        return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)

    def scan_metadata(self) -> list[DatasetInfo]:
        with self._connection() as connection:
            datasets = []
            for raw_name in connection.tables():
                name = self._decode(raw_name)
                families = connection.table(name).families()
                datasets.append(
                    DatasetInfo(
                        datasource_id=self.datasource_id,
                        name=name,
                        kind="table",
                        fields=[
                            FieldInfo(name=self._decode(family), data_type="column_family")
                            for family in sorted(families)
                        ],
                    )
                )
            return datasets

    def _rows(self, table_name: str, limit: int, row_prefix: str | None = None):
        with self._connection() as connection:
            table = connection.table(table_name)
            prefix = row_prefix.encode() if row_prefix else None
            rows = []
            for key, values in table.scan(row_prefix=prefix, limit=limit):
                row = {
                    self._decode(column): normalize_sample_value(value)
                    for column, value in values.items()
                }
                rows.append({"row_key": self._decode(key), **row})
            return rows

    def scan_samples(self, datasets: list[DatasetInfo], limit: int = 3) -> list[DatasetSample]:
        bounded = max(0, min(limit, 3))
        return [
            DatasetSample(
                datasource_id=self.datasource_id,
                dataset=dataset.name,
                rows=self._rows(dataset.name, bounded),
            )
            for dataset in datasets
        ]

    def scan_profile_records(
        self, datasets: list[DatasetInfo], limit: int = 20
    ) -> dict[str, list[dict[str, Any]]]:
        bounded = max(0, min(limit, 20))
        return {dataset.name: self._rows(dataset.name, bounded) for dataset in datasets}

    def execute(self, query: str, max_rows: int = 200) -> NormalizedResult:
        payload = parse_query_payload(query)
        table = payload.get("table")
        if not isinstance(table, str) or not table:
            raise ValueError("HBase query requires table")
        limit = bounded_limit(payload.get("limit"), max_rows)
        rows = self._rows(table, limit + 1, payload.get("row_prefix"))
        truncated = len(rows) > limit
        rows = rows[:limit]
        columns = sorted({key for row in rows for key in row})
        return NormalizedResult(
            source=self.datasource_id,
            dataset=table,
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
        )
