from __future__ import annotations

import sqlite3

from qaneris.adapters.relational.sqlite import SQLiteAdapter
from qaneris.contracts import (
    AggregateFunction,
    FilterOperator,
    GroundedDataObjectRef,
    GroundedFieldRef,
    GroundedQueryPlan,
    PlanAggregate,
    PlanFilter,
    PlanJoin,
    PlanSort,
    PlanSortTarget,
    QueryResultType,
    SortDirection,
    TimeRange,
)
from qaneris.querying.generation import GroundedSQLCompiler
from qaneris.querying.validation import GroundedNativeQueryValidator


def ref(object_id: str, name: str) -> GroundedFieldRef:
    return GroundedFieldRef(datasource_id="ds", data_object_id=object_id, field_path=name)


def test_real_sqlite_parameterized_acceptance(tmp_path) -> None:
    database = tmp_path / "sales.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            "CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT);"
            "CREATE TABLE orders(id INTEGER PRIMARY KEY, customer_id INTEGER, region TEXT, amount REAL, ordered_at TEXT);"
            "INSERT INTO customers VALUES (1, 'Acme'), (2, 'Globex');"
        )
        connection.executemany(
            "INSERT INTO orders VALUES (?, ?, ?, ?, ?)",
            [
                (1, 1, "东京", 100, "2026-01-10"),
                (2, 1, "东京", 50, "2026-02-01"),
                (3, 2, "大阪", 75, "2026-01-15"),
                (4, 2, "Robert'); DROP TABLE users; --", 5, "2026-01-20"),
            ],
        )
    objects = {
        name: GroundedDataObjectRef(datasource_id="ds", data_object_id=name, name=name)
        for name in ("orders", "customers")
    }
    plan = GroundedQueryPlan(
        plan_id="acceptance",
        workspace_id="default",
        datasource_id="ds",
        data_object_ids=["orders", "customers"],
        data_objects=objects,
        scan_version=7,
        selected_fields=[ref("customers", "name")],
        aggregates=[
            PlanAggregate(
                function=AggregateFunction.SUM, field=ref("orders", "amount"), alias="sales"
            )
        ],
        filters=[
            PlanFilter(
                field=ref("orders", "region"), operator=FilterOperator.IN, value=["东京", "大阪"]
            )
        ],
        group_by=[ref("customers", "name")],
        sorts=[
            PlanSort(
                target=PlanSortTarget.AGGREGATE,
                aggregate_alias="sales",
                direction=SortDirection.DESCENDING,
            )
        ],
        joins=[
            PlanJoin(
                datasource_id="ds",
                relationship_id="fk",
                from_data_object_id="orders",
                from_field_path="customer_id",
                to_data_object_id="customers",
                to_field_path="id",
            )
        ],
        time_field=ref("orders", "ordered_at"),
        time_range=TimeRange(start="2026-01-01", end="2026-01-31"),
        limit=2,
        expected_result_type=QueryResultType.TABULAR,
    )
    native = GroundedSQLCompiler().compile(plan)
    GroundedNativeQueryValidator().validate(native, plan)
    result = SQLiteAdapter("ds", {"path": str(database)}).execute_bound(
        native.command, native.parameters, 2
    )
    assert result.rows == [{"name": "Acme", "sales": 100.0}, {"name": "Globex", "sales": 75.0}]

    attack = "Robert'); DROP TABLE users; --"
    attack_plan = plan.model_copy(
        update={
            "plan_id": "attack",
            "data_object_ids": ["orders"],
            "data_objects": {"orders": objects["orders"]},
            "selected_fields": [],
            "group_by": [],
            "joins": [],
            "sorts": [],
            "time_field": None,
            "time_range": None,
            "filters": [
                PlanFilter(
                    field=ref("orders", "region"), operator=FilterOperator.EQUALS, value=attack
                )
            ],
            "limit": 10,
            "expected_result_type": QueryResultType.SCALAR,
        }
    )
    attack_native = GroundedSQLCompiler().compile(attack_plan)
    assert attack not in attack_native.command
    assert attack in attack_native.parameters
    SQLiteAdapter("ds", {"path": str(database)}).execute_bound(
        attack_native.command, attack_native.parameters
    )
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 4
