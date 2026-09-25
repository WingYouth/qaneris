"""Shared SQLAlchemy relational adapter."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from qaneris.adapters.base import DataSourceAdapter
from qaneris.adapters.native_values import normalize_sample_row
from qaneris.contracts import (
    ConstraintInfo,
    DatasetInfo,
    DatasetSample,
    FieldInfo,
    IndexInfo,
    NormalizedResult,
    RelationInfo,
)
from qaneris.querying.validation.read_only import validate_read_only_query


class SQLAlchemyAdapter(DataSourceAdapter):
    """Shared adapter for SQL databases supported by a SQLAlchemy dialect."""

    def _database_url(self) -> str:
        url = self.connection.get("url")
        if not isinstance(url, str) or not url.strip():
            raise ValueError("SQL database connection requires a SQLAlchemy url")
        return url

    @contextmanager
    def _connect(self) -> Iterator[Any]:
        try:
            from sqlalchemy import create_engine, text
        except ImportError as error:
            raise RuntimeError(
                "SQL database support requires the matching Qaneris database extra"
            ) from error
        # ``connect_args`` carries driver TLS parameters the materializer produced (psycopg's
        # sslmode/sslrootcert, clickhouse-connect's ca_cert/...). It is empty for a non-TLS
        # connection, so a connection without TLS keeps exactly its current behavior.
        engine = create_engine(
            self._database_url(),
            pool_pre_ping=True,
            connect_args=self.connection.get("connect_args", {}),
        )
        try:
            with engine.connect() as connection:
                if self.connection.get("driver") == "clickhouse":
                    native_execute = connection.execute

                    def execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
                        if isinstance(statement, str):
                            statement = text(statement)
                        return native_execute(statement, *args, **kwargs)

                    connection.execute = execute
                yield connection
        finally:
            engine.dispose()

    def test_connection(self) -> None:
        with self._connect() as connection:
            from sqlalchemy import text

            connection.execute(text("SELECT 1"))

    @staticmethod
    def _dataset_name(schema: str | None, table: str) -> str:
        return f"{schema}.{table}" if schema else table

    @staticmethod
    def _split_dataset(name: str) -> tuple[str | None, str]:
        if "." not in name:
            return None, name
        return tuple(name.split(".", 1))  # type: ignore[return-value]

    @staticmethod
    def _schemas(inspector: Any) -> list[str | None]:
        default = getattr(inspector, "default_schema_name", None)
        try:
            schemas = inspector.get_schema_names()
        except NotImplementedError:
            return [default]
        ignored = {
            "information_schema",
            "pg_catalog",
            "pg_toast",
            "sys",
            "mysql",
            "performance_schema",
            "_timescaledb_catalog",
            "_timescaledb_internal",
            "_timescaledb_cache",
            "timescaledb_experimental",
        }
        usable = [
            schema
            for schema in schemas
            if schema.lower() not in ignored
            and not schema.lower().startswith("_timescaledb_")
            and schema.lower() not in {
                "timescaledb_experimental",
                "timescaledb_information",
            }
        ]
        return usable or [default]

    def scan_metadata(self) -> list[DatasetInfo]:
        datasets: list[DatasetInfo] = []
        with self._connect() as connection:
            from sqlalchemy import inspect

            inspector = inspect(connection)
            for schema in self._schemas(inspector):
                tables = [(name, "table") for name in inspector.get_table_names(schema=schema)]
                try:
                    tables.extend(
                        (name, "view") for name in inspector.get_view_names(schema=schema)
                    )
                except NotImplementedError:
                    pass
                for table, kind in tables:
                    pk = inspector.get_pk_constraint(table, schema=schema) or {}
                    primary_keys = set(
                        pk.get(
                            "constrained_columns", []
                        )
                        or []
                    )
                    try:
                        raw_indexes = inspector.get_indexes(table, schema=schema)
                    except NotImplementedError:
                        raw_indexes = []
                    indexes = [
                        IndexInfo(
                            name=item.get("name") or f"index_{table}_{position}",
                            fields=item.get("column_names") or [],
                            unique=bool(item.get("unique", False)),
                            index_type=item.get("type"),
                            native_options=item.get("dialect_options") or {},
                        )
                        for position, item in enumerate(raw_indexes)
                    ]
                    indexed_fields = {field for item in indexes for field in item.fields}
                    unique_fields = {
                        field for item in indexes if item.unique for field in item.fields
                    }
                    fields = [
                        FieldInfo(
                            name=column["name"],
                            data_type=str(column.get("type") or "unknown"),
                            native_type=str(column.get("type") or "unknown"),
                            nullable=bool(column.get("nullable", True)),
                            primary_key=column["name"] in primary_keys,
                            unique=column["name"] in unique_fields,
                            indexed=column["name"] in indexed_fields or column["name"] in primary_keys,
                            default_value=column.get("default"),
                            comment=column.get("comment"),
                        )
                        for column in inspector.get_columns(table, schema=schema)
                    ]
                    constraints = []
                    if primary_keys:
                        constraints.append(ConstraintInfo(
                            name=pk.get("name") or f"pk_{table}", constraint_type="primary_key",
                            fields=list(pk.get("constrained_columns") or []),
                        ))
                    try:
                        unique_constraints = inspector.get_unique_constraints(table, schema=schema)
                    except NotImplementedError:
                        unique_constraints = []
                    constraints.extend(
                        ConstraintInfo(
                            name=item.get("name") or f"unique_{table}_{position}",
                            constraint_type="unique",
                            fields=item.get("column_names") or [],
                        )
                        for position, item in enumerate(unique_constraints)
                    )
                    try:
                        checks = inspector.get_check_constraints(table, schema=schema)
                    except NotImplementedError:
                        checks = []
                    constraints.extend(
                        ConstraintInfo(name=item.get("name") or f"check_{table}_{position}",
                                       constraint_type="check", expression=item.get("sqltext"))
                        for position, item in enumerate(checks)
                    )
                    try:
                        foreign_keys = inspector.get_foreign_keys(table, schema=schema)
                    except NotImplementedError:
                        foreign_keys = []
                    constraints.extend(
                        ConstraintInfo(
                            name=item.get("name") or f"fk_{table}_{position}",
                            constraint_type="foreign_key",
                            fields=item.get("constrained_columns") or [],
                            expression=(
                                f"{item.get('referred_schema') + '.' if item.get('referred_schema') else ''}"
                                f"{item.get('referred_table')}({', '.join(item.get('referred_columns') or [])})"
                            ),
                        )
                        for position, item in enumerate(foreign_keys)
                    )
                    datasets.append(
                        DatasetInfo(
                            datasource_id=self.datasource_id,
                            name=self._dataset_name(schema, table),
                            kind=kind,
                            namespace=schema,
                            fields=fields,
                            indexes=indexes,
                            constraints=constraints,
                        )
                    )
        return datasets

    def scan_relations(self) -> list[RelationInfo]:
        relations: list[RelationInfo] = []
        with self._connect() as connection:
            from sqlalchemy import inspect

            inspector = inspect(connection)
            for schema in self._schemas(inspector):
                for table in inspector.get_table_names(schema=schema):
                    try:
                        foreign_keys = inspector.get_foreign_keys(table, schema=schema)
                    except NotImplementedError:
                        foreign_keys = []
                    for foreign_key in foreign_keys:
                        referred_schema = foreign_key.get("referred_schema") or schema
                        referred_table = foreign_key.get("referred_table")
                        constrained = foreign_key.get("constrained_columns") or []
                        referred = foreign_key.get("referred_columns") or []
                        if not referred_table:
                            continue
                        for from_field, to_field in zip(constrained, referred, strict=False):
                            relations.append(
                                RelationInfo(
                                    datasource_id=self.datasource_id,
                                    from_dataset=self._dataset_name(schema, table),
                                    from_field=from_field,
                                    to_dataset=self._dataset_name(referred_schema, referred_table),
                                    to_field=to_field,
                                    relation_type="foreign_key",
                                    source="scan",
                                )
                            )
        return relations

    def scan_samples(self, datasets: list[DatasetInfo], limit: int = 3) -> list[DatasetSample]:
        bounded_limit = max(0, min(limit, 3))
        samples: list[DatasetSample] = []
        with self._connect() as connection:
            from sqlalchemy import MetaData, Table, select

            for dataset in datasets:
                schema, table_name = self._split_dataset(dataset.name)
                table = Table(table_name, MetaData(), schema=schema, autoload_with=connection)
                rows = connection.execute(select(table).limit(bounded_limit)).fetchall()
                samples.append(
                    DatasetSample(
                        datasource_id=self.datasource_id,
                        dataset=dataset.name,
                        rows=[normalize_sample_row(dict(row._mapping)) for row in rows],
                    )
                )
        return samples

    def scan_profile_records(
        self, datasets: list[DatasetInfo], limit: int = 20
    ) -> dict[str, list[dict[str, Any]]]:
        bounded_limit = max(0, min(limit, 20))
        records: dict[str, list[dict[str, Any]]] = {}
        with self._connect() as connection:
            from sqlalchemy import MetaData, Table, select

            for dataset in datasets:
                schema, table_name = self._split_dataset(dataset.name)
                table = Table(table_name, MetaData(), schema=schema, autoload_with=connection)
                rows = connection.execute(select(table).limit(bounded_limit)).fetchall()
                records[dataset.name] = [
                    normalize_sample_row(dict(row._mapping)) for row in rows
                ]
        return records

    def execute(self, query: str, max_rows: int = 200) -> NormalizedResult:
        validate_read_only_query(query)
        with self._connect() as connection:
            from sqlalchemy import text

            result = connection.execute(text(query))
            rows = result.fetchmany(max_rows + 1)
            columns = list(result.keys())
        truncated = len(rows) > max_rows
        return NormalizedResult(
            source=self.datasource_id,
            dataset="query",
            columns=columns,
            rows=[normalize_sample_row(dict(row._mapping)) for row in rows[:max_rows]],
            row_count=min(len(rows), max_rows),
            truncated=truncated,
        )

    def execute_bound(
        self, query: str, parameters: tuple[Any, ...], max_rows: int = 200
    ) -> NormalizedResult:
        if self.connection.get("driver") not in {"postgresql", "mysql"}:
            raise ValueError("formal grounded execution is not enabled for this driver")
        validate_read_only_query(query)
        if max_rows < 1:
            raise ValueError("max_rows must be positive")
        with self._connect() as connection:
            result = connection.exec_driver_sql(query, parameters)
            rows = result.fetchmany(max_rows + 1)
            columns = list(result.keys())
        return NormalizedResult(
            source=self.datasource_id,
            dataset="query",
            columns=columns,
            rows=[normalize_sample_row(dict(row._mapping)) for row in rows[:max_rows]],
            row_count=min(len(rows), max_rows),
            truncated=len(rows) > max_rows,
        )
