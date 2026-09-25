"""InfluxDB time-series adapter."""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from qaneris.adapters.base import DataSourceAdapter
from qaneris.adapters.native_values import normalize_sample_row
from qaneris.contracts import DatasetInfo, DatasetSample, FieldInfo, NormalizedResult

WRITE_FLUX = re.compile(r"\b(to|experimental\.to)\s*\(", re.IGNORECASE)


class InfluxDBAdapter(DataSourceAdapter):
    @contextmanager
    def _client(self) -> Iterator[Any]:
        try:
            from influxdb_client import InfluxDBClient
        except ImportError as error:
            raise RuntimeError("InfluxDB support requires qaneris-platform[influxdb]") from error
        required = ("url", "token", "org", "bucket")
        missing = [name for name in required if not self.connection.get(name)]
        if missing:
            raise ValueError(f"InfluxDB connection missing: {', '.join(missing)}")
        # TLS parameters (verify_ssl, ssl_ca_cert, cert_file, cert_key_file) come from the
        # materializer; the URL it supplies is already https when TLS is on.
        tls_options: dict[str, Any] = {}
        for name in ("verify_ssl", "ssl_ca_cert", "cert_file", "cert_key_file"):
            if self.connection.get(name) is not None:
                tls_options[name] = self.connection[name]
        # The runtime key was unlocked and normalized to unencrypted PKCS#8, so the password is
        # explicitly None - which is a value to pass, not an absent parameter to skip.
        if "cert_key_file" in tls_options:
            tls_options["cert_key_password"] = self.connection.get("cert_key_password")
        client = InfluxDBClient(
            url=self.connection["url"],
            token=self.connection["token"],
            org=self.connection["org"],
            **tls_options,
        )
        try:
            yield client
        finally:
            client.close()

    def test_connection(self) -> None:
        self._measurements()

    def _query(self, query: str) -> list[dict[str, Any]]:
        with self._client() as client:
            tables = client.query_api().query(query=query, org=self.connection["org"])
            return [
                normalize_sample_row(record.values) for table in tables for record in table.records
            ]

    def _measurements(self) -> list[str]:
        bucket = self.connection["bucket"]
        rows = self._query(
            f'import "influxdata/influxdb/schema"\nschema.measurements(bucket: "{bucket}")'
        )
        return sorted(str(row.get("_value")) for row in rows if row.get("_value") is not None)

    def scan_metadata(self) -> list[DatasetInfo]:
        bucket = self.connection["bucket"]
        datasets = []
        for measurement in self._measurements():
            rows = self._query(
                f'from(bucket: "{bucket}") |> range(start: 0) '
                f'|> filter(fn: (r) => r._measurement == "{measurement}") '
                '|> keep(columns: ["_field"]) '
                '|> distinct(column: "_field")'
            )
            datasets.append(
                DatasetInfo(
                    datasource_id=self.datasource_id,
                    name=measurement,
                    kind="measurement",
                    fields=[
                        FieldInfo(name=str(row["_value"]), data_type="field")
                        for row in rows
                        if row.get("_value") is not None
                    ],
                )
            )
        return datasets

    def scan_samples(self, datasets: list[DatasetInfo], limit: int = 3) -> list[DatasetSample]:
        bounded = max(0, min(limit, 3))
        bucket = self.connection["bucket"]
        return [
            DatasetSample(
                datasource_id=self.datasource_id,
                dataset=dataset.name,
                rows=self._query(
                    f'from(bucket: "{bucket}") |> range(start: 0) '
                    f'|> filter(fn: (r) => r._measurement == "{dataset.name}") '
                    f"|> limit(n: {bounded})"
                )[:bounded],
            )
            for dataset in datasets
        ]

    def scan_profile_records(
        self, datasets: list[DatasetInfo], limit: int = 20
    ) -> dict[str, list[dict[str, Any]]]:
        bounded = max(0, min(limit, 20))
        bucket = self.connection["bucket"]
        return {
            dataset.name: self._query(
                f'from(bucket: "{bucket}") |> range(start: 0) '
                f'|> filter(fn: (r) => r._measurement == "{dataset.name}") '
                f"|> limit(n: {bounded})"
            )
            for dataset in datasets
        }

    def execute(self, query: str, max_rows: int = 200) -> NormalizedResult:
        if WRITE_FLUX.search(query):
            raise ValueError("InfluxDB write pipelines are not allowed")
        query = query.replace("__configured_bucket__", str(self.connection["bucket"]))
        rows = self._query(f"{query}\n|> limit(n: {max_rows + 1})")
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
        columns = sorted({key for row in rows for key in row})
        return NormalizedResult(
            source=self.datasource_id,
            dataset="query",
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
        )
