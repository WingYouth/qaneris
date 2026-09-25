from __future__ import annotations

import pytest
from sqlalchemy.exc import ObjectNotExecutableError

from smartdata.adapters.relational.sqlalchemy import SQLAlchemyAdapter


def test_clickhouse_connection_coerces_raw_sql_strings_for_sqlalchemy_2() -> None:
    adapter = SQLAlchemyAdapter(
        "ds_clickhouse",
        {"driver": "clickhouse", "url": "sqlite:///:memory:"},
    )

    with adapter._connect() as connection:
        assert connection.execute("SELECT 1").scalar_one() == 1


def test_non_clickhouse_connection_keeps_sqlalchemy_2_raw_string_behavior() -> None:
    adapter = SQLAlchemyAdapter(
        "ds_sqlite",
        {"driver": "sqlite", "url": "sqlite:///:memory:"},
    )

    with adapter._connect() as connection, pytest.raises(ObjectNotExecutableError):
        connection.execute("SELECT 1")


def test_timescaledb_internal_schemas_are_excluded_from_scan() -> None:
    class Inspector:
        default_schema_name = "public"

        def get_schema_names(self) -> list[str]:
            return [
                "public",
                "_timescaledb_catalog",
                "_timescaledb_internal",
                "_timescaledb_cache",
                "_timescaledb_config",
                "_timescaledb_debug",
                "_timescaledb_functions",
                "timescaledb_experimental",
                "timescaledb_information",
                "information_schema",
            ]

    assert SQLAlchemyAdapter._schemas(Inspector()) == ["public"]
