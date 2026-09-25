from __future__ import annotations

import pytest

from qaneris.common.errors import ExecutionValidationError, QueryPlanningError, QuerySafetyError
from qaneris.contracts import (
    AggregateFunction,
    FilterOperator,
    GroundedDataObjectRef,
    GroundedFieldRef,
    GroundedQueryPlan,
    NativeQuery,
    PlanAggregate,
    PlanFilter,
    PlanSort,
    PlanSortTarget,
    QueryLanguage,
    QueryResultType,
    SortDirection,
    TimeRange,
)
from qaneris.querying.generation import GroundedSQLCompiler
from qaneris.querying.validation import ExecutionRevisionValidator, GroundedNativeQueryValidator


def field(name: str, object_id: str = "orders") -> GroundedFieldRef:
    return GroundedFieldRef(datasource_id="ds", data_object_id=object_id, field_path=name)


def plan(**changes) -> GroundedQueryPlan:
    values = {
        "plan_id": "plan-1",
        "workspace_id": "default",
        "datasource_id": "ds",
        "data_object_ids": ["orders"],
        "data_objects": {
            "orders": GroundedDataObjectRef(
                datasource_id="ds", data_object_id="orders", name="orders", object_kind="table"
            )
        },
        "scan_version": 3,
        "aggregates": [
            PlanAggregate(function=AggregateFunction.SUM, field=field("amount"), alias="sales")
        ],
        "expected_result_type": QueryResultType.SCALAR,
        "limit": 200,
    }
    values.update(changes)
    return GroundedQueryPlan(**values)


def test_deterministic_metric_dimension_filter_ranking_and_binding() -> None:
    source = plan(
        selected_fields=[field("region")],
        group_by=[field("region")],
        filters=[PlanFilter(field=field("region"), operator=FilterOperator.EQUALS, value="东京")],
        sorts=[
            PlanSort(
                target=PlanSortTarget.AGGREGATE,
                aggregate_alias="sales",
                direction=SortDirection.DESCENDING,
            )
        ],
        limit=10,
        expected_result_type=QueryResultType.TABULAR,
    )
    first = GroundedSQLCompiler().compile(source)
    second = GroundedSQLCompiler().compile(source)
    assert first == second
    assert first.parameters == ("东京", 10)
    assert "东京" not in first.command
    assert 'GROUP BY t0."region" ORDER BY "sales" DESC LIMIT ?' in first.command
    GroundedNativeQueryValidator().validate(first, source)


@pytest.mark.parametrize(
    ("operator", "value", "fragment", "parameters"),
    [
        (FilterOperator.IN, ["东京", "大阪"], "IN (?, ?)", ("东京", "大阪", 200)),
        (FilterOperator.BETWEEN, [10, 20], "BETWEEN ? AND ?", (10, 20, 200)),
        (FilterOperator.CONTAINS, "京", "LIKE ?", ("%京%", 200)),
        (FilterOperator.EQUALS, None, "IS NULL", (200,)),
        (FilterOperator.NOT_EQUALS, None, "IS NOT NULL", (200,)),
    ],
)
def test_filter_forms(operator, value, fragment, parameters) -> None:
    native = GroundedSQLCompiler().compile(
        plan(filters=[PlanFilter(field=field("region"), operator=operator, value=value)])
    )
    assert fragment in native.command
    assert native.parameters == parameters


def test_time_range_and_field_sort() -> None:
    date_field = field("ordered_at")
    native = GroundedSQLCompiler().compile(
        plan(
            time_field=date_field,
            time_range=TimeRange(start="2026-01-01", end="2026-01-31"),
            sorts=[PlanSort(target=PlanSortTarget.FIELD, field=date_field)],
        )
    )
    assert 't0."ordered_at" >= ? AND t0."ordered_at" <= ?' in native.command
    assert native.parameters == ("2026-01-01", "2026-01-31", 200)


def test_namespace_identity_and_missing_locator_fail_closed() -> None:
    public = GroundedSQLCompiler().compile(
        plan(
            data_objects={
                "orders": GroundedDataObjectRef(
                    datasource_id="ds", data_object_id="orders", name="orders", namespace="public"
                )
            }
        )
    )
    archive = GroundedSQLCompiler().compile(
        plan(
            data_objects={
                "orders": GroundedDataObjectRef(
                    datasource_id="ds", data_object_id="orders", name="orders", namespace="archive"
                )
            }
        )
    )
    assert '"public"."orders"' in public.command
    assert '"archive"."orders"' in archive.command
    with pytest.raises(QueryPlanningError, match="Missing native locator"):
        GroundedSQLCompiler().compile(
            plan(
                data_objects={
                    "orders": GroundedDataObjectRef(datasource_id="ds", data_object_id="orders")
                }
            )
        )


def test_parameter_count_writes_multistatement_and_revision_are_rejected() -> None:
    source = plan()
    native = GroundedSQLCompiler().compile(source)
    validator = GroundedNativeQueryValidator()
    with pytest.raises(QuerySafetyError, match="placeholder"):
        validator.validate(native.model_copy(update={"parameters": ()}), source)
    for command in ["DELETE FROM orders", "SELECT 1; DROP TABLE orders"]:
        unsafe = NativeQuery(
            datasource_id="ds",
            plan_id="plan-1",
            scan_version=3,
            query_language=QueryLanguage.SQL,
            command=command,
            display_command=command,
        )
        with pytest.raises(QuerySafetyError):
            validator.validate(unsafe, source)
    with pytest.raises(ExecutionValidationError, match="stale"):
        ExecutionRevisionValidator().validate(native, 4)


# ----------------------------------------------------------------------
# Native query provenance (Phase 1)
# ----------------------------------------------------------------------


def test_native_query_provenance_rejects_same_identifiers_with_a_different_command() -> None:
    """A forged native query sharing plan_id / datasource_id / scan_version / query_language
    is rejected because its ``command`` (and therefore its provenance) does not match the
    compiled native query."""
    source = plan()
    compiled = GroundedSQLCompiler().compile(source)
    validator = GroundedNativeQueryValidator()

    forged_with_placeholder = compiled.model_copy(
        update={
            "command": "SELECT * FROM customers AS t0 WHERE t0.region = ?",
            "display_command": "SELECT * FROM customers AS t0 WHERE t0.region = ?",
            "parameters": (),
        }
    )
    # Without ``expected`` the validator catches the placeholder mismatch first.
    with pytest.raises(QuerySafetyError, match="placeholder"):
        validator.validate(forged_with_placeholder, source)

    forged = compiled.model_copy(
        update={
            "command": "SELECT * FROM customers AS t0",
            "display_command": "SELECT * FROM customers AS t0",
            "parameters": (),
        }
    )
    # Same identifier fingerprint but a different command — provenance catches it.
    with pytest.raises(QuerySafetyError, match="provenance"):
        validator.validate(forged, source, expected=compiled)


def test_native_query_provenance_rejects_forged_parameters() -> None:
    source = plan()
    compiled = GroundedSQLCompiler().compile(source)
    validator = GroundedNativeQueryValidator()

    # Keep the placeholder count matching the command so only the provenance
    # check can catch the parameter swap.
    forged = compiled.model_copy(update={"parameters": (999,)})
    assert forged.command == compiled.command
    assert forged.parameters != compiled.parameters
    with pytest.raises(QuerySafetyError, match="provenance"):
        validator.validate(forged, source, expected=compiled)


def test_native_query_provenance_accepts_a_bit_identical_recompilation() -> None:
    source = plan()
    compiled = GroundedSQLCompiler().compile(source)
    # The compiler is deterministic; a second compilation must be byte-equal.
    again = GroundedSQLCompiler().compile(source)
    GroundedNativeQueryValidator().validate(again, source, expected=compiled)


# ----------------------------------------------------------------------
# Read-only SQL safety (Phase 1)
# ----------------------------------------------------------------------


from qaneris.querying.validation.read_only import (
    UnsafeQueryError,
    validate_read_only_query,
)


@pytest.mark.parametrize(
    "command",
    [
        # Quoted identifiers that happen to spell a write keyword must pass.
        'SELECT t0."delete" FROM "orders" AS t0',
        'SELECT t0."drop" FROM "orders" AS t0',
        "SELECT t0.`delete` FROM `orders` AS t0",
        # The keyword hiding inside a string literal must not be treated as a
        # statement operation either.
        "SELECT 'this is not a delete' AS label",
        # A keyword inside a line or block comment must not be a write.
        "SELECT 1 -- delete from orders\n FROM dual",
        "SELECT 1 /* delete from orders */ FROM dual",
        # Leading whitespace before SELECT/WITH is allowed.
        "  SELECT 1",
        "\nWITH x AS (SELECT 1) SELECT * FROM x",
    ],
)
def test_read_only_validator_accepts_safe_queries(command: str) -> None:
    validate_read_only_query(command)


@pytest.mark.parametrize(
    "command",
    [
        # DML
        "DELETE FROM orders",
        "UPDATE orders SET amount = 0",
        "INSERT INTO orders VALUES (1, 'a')",
        "REPLACE INTO orders VALUES (1, 'a')",
        # DDL
        "DROP TABLE orders",
        "ALTER TABLE orders ADD COLUMN foo TEXT",
        "CREATE TABLE foo (id INTEGER)",
        # SQLite metadata mutations
        "ATTACH DATABASE 'other.db' AS other",
        "DETACH DATABASE other",
        "PRAGMA writable_schema = ON",
        "VACUUM",
        "REINDEX",
        "ANALYZE",
        # Multi-statement: a hidden write after a SELECT is still a write.
        "SELECT 1; DELETE FROM orders",
        # Two stacked read-only statements are still two statements.
        "SELECT 1; SELECT 2",
        # A semicolon inside a string literal does not cancel the real separator.
        "SELECT ';' AS s; SELECT 1",
        # A write hidden inside a CTE / sub-statement — still refused at
        # the whole-statement level.
        "WITH x AS (DELETE FROM orders RETURNING id) SELECT * FROM x",
        # A query that is *only* a comment has no statement.
        "/* delete from orders */",
        "-- delete from orders\n",
    ],
)
def test_read_only_validator_rejects_unsafe_queries(command: str) -> None:
    with pytest.raises(UnsafeQueryError):
        validate_read_only_query(command)
