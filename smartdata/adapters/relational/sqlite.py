"""SQLite relational adapter."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote

from smartdata.adapters.base import DataSourceAdapter
from smartdata.adapters.native_values import normalize_sample_row
from smartdata.contracts import (
    ConstraintInfo,
    DatasetInfo,
    DatasetSample,
    FieldInfo,
    IndexInfo,
    NormalizedResult,
    RelationInfo,
)
from smartdata.querying.validation.read_only import validate_read_only_query


class SQLiteAdapter(DataSourceAdapter):
    def _path(self) -> Path:
        raw = self.connection.get("path")
        if not raw:
            raise ValueError("SQLite connection requires a path")
        path = Path(str(raw)).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"SQLite database does not exist: {path}")
        return path

    def _connect(self) -> sqlite3.Connection:
        uri = f"file:{quote(self._path().as_posix(), safe='/:')}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection

    def test_connection(self) -> None:
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()

    def scan_metadata(self) -> list[DatasetInfo]:
        datasets: list[DatasetInfo] = []
        with self._connect() as connection:
            tables = connection.execute(
                "SELECT name, type FROM sqlite_master "
                "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            for table in tables:
                escaped = table["name"].replace('"', '""')
                table_info = connection.execute(f'PRAGMA table_info("{escaped}")').fetchall()
                indexes: list[IndexInfo] = []
                indexed_fields: set[str] = set()
                unique_fields: set[str] = set()
                for index_row in connection.execute(f'PRAGMA index_list("{escaped}")').fetchall():
                    index_name = index_row["name"]
                    escaped_index = index_name.replace('"', '""')
                    index_fields = [
                        row["name"]
                        for row in connection.execute(f'PRAGMA index_info("{escaped_index}")')
                        if row["name"] is not None
                    ]
                    indexed_fields.update(index_fields)
                    if index_row["unique"]:
                        unique_fields.update(index_fields)
                    indexes.append(IndexInfo(name=index_name, fields=index_fields,
                                             unique=bool(index_row["unique"]),
                                             index_type="btree"))
                fields = [
                    FieldInfo(
                        name=row["name"],
                        data_type=row["type"] or "unknown",
                        native_type=row["type"] or None,
                        nullable=not bool(row["notnull"]),
                        primary_key=bool(row["pk"]),
                        unique=row["name"] in unique_fields,
                        indexed=row["name"] in indexed_fields or bool(row["pk"]),
                        default_value=row["dflt_value"],
                    )
                    for row in table_info
                ]
                pk_fields = [row["name"] for row in table_info if row["pk"]]
                constraints = (
                    [ConstraintInfo(name=f"pk_{table['name']}", constraint_type="primary_key",
                                    fields=pk_fields)] if pk_fields else []
                )
                for foreign_key in connection.execute(f'PRAGMA foreign_key_list("{escaped}")'):
                    constraints.append(
                        ConstraintInfo(
                            name=f"fk_{table['name']}_{foreign_key['id']}",
                            constraint_type="foreign_key",
                            fields=[foreign_key["from"]],
                            expression=f"{foreign_key['table']}({foreign_key['to']})",
                        )
                    )
                datasets.append(
                    DatasetInfo(
                        datasource_id=self.datasource_id,
                        name=table["name"],
                        kind=table["type"],
                        fields=fields,
                        indexes=indexes,
                        constraints=constraints,
                    )
                )
        return datasets

    def scan_relations(self) -> list[RelationInfo]:
        relations: list[RelationInfo] = []
        with self._connect() as connection:
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            for table in tables:
                escaped = table["name"].replace('"', '""')
                for row in connection.execute(f'PRAGMA foreign_key_list("{escaped}")'):
                    relations.append(
                        RelationInfo(
                            datasource_id=self.datasource_id,
                            from_dataset=table["name"],
                            from_field=row["from"] or None,
                            to_dataset=row["table"],
                            to_field=row["to"] or None,
                            relation_type="foreign_key",
                            source="scan",
                        )
                    )
        return relations

    def scan_samples(self, datasets: list[DatasetInfo], limit: int = 3) -> list[DatasetSample]:
        bounded_limit = max(0, min(limit, 3))
        samples: list[DatasetSample] = []
        with self._connect() as connection:
            for dataset in datasets:
                escaped = dataset.name.replace('"', '""')
                rows = connection.execute(
                    f'SELECT * FROM "{escaped}" LIMIT ?', (bounded_limit,)
                ).fetchall()
                samples.append(
                    DatasetSample(
                        datasource_id=self.datasource_id,
                        dataset=dataset.name,
                        rows=[normalize_sample_row(dict(row)) for row in rows],
                    )
                )
        return samples

    def scan_profile_records(
        self, datasets: list[DatasetInfo], limit: int = 20
    ) -> dict[str, list[dict[str, Any]]]:
        bounded_limit = max(0, min(limit, 20))
        records: dict[str, list[dict[str, Any]]] = {}
        with self._connect() as connection:
            for dataset in datasets:
                escaped = dataset.name.replace('"', '""')
                rows = connection.execute(
                    f'SELECT * FROM "{escaped}" LIMIT ?', (bounded_limit,)
                ).fetchall()
                records[dataset.name] = [normalize_sample_row(dict(row)) for row in rows]
        return records

    def execute(self, query: str, max_rows: int = 200) -> NormalizedResult:
        validate_read_only_query(query)
        return self.execute_bound(query, (), max_rows)

    def execute_bound(
        self, query: str, parameters: tuple[Any, ...], max_rows: int = 200
    ) -> NormalizedResult:
        validate_read_only_query(query)
        with self._connect() as connection:
            cursor = connection.execute(query, parameters)
            rows = cursor.fetchmany(max_rows + 1)
            columns = [column[0] for column in cursor.description or []]
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
        dataset = "query"
        return NormalizedResult(
            source=self.datasource_id,
            dataset=dataset,
            columns=columns,
            rows=[dict(row) for row in rows],
            row_count=len(rows),
            truncated=truncated,
        )
