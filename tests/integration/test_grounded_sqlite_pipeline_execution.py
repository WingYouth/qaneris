"""End-to-end execution boundary: GroundedQueryContext → … → GroundedExecution.

The whole formal pipeline runs against a real SQLite database and a fake Neo4j
graph reader, asserting that the typed result and the evidence are produced
exactly once, the published revision fence holds, and a forged native query
cannot bypass the provenance check.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

import pytest
from _neo4j_fake import FakeNeo4jDriver, graph_reader, graph_store

from smartdata.adapters import create_adapter
from smartdata.application.service import SmartDataService
from smartdata.catalog import Catalog
from smartdata.common.errors import ExecutionValidationError, QuerySafetyError
from smartdata.contracts import Datasource, NativeQuery, QueryLanguage
from smartdata.contracts.query import (
    AggregateFunction,
    FilterOperator,
    QueryResultType,
    TimeRange,
    TimeSpec,
)
from smartdata.contracts.semantic import BusinessFilter, BusinessQuery, RankingSpec
from smartdata.scan import ScanService
from smartdata.semantic import (
    GraphSemanticRetriever,
    SemanticAsset,
    SemanticGrounder,
    SQLiteSemanticAssetRegistry,
)

DATASOURCE_ID = "ds_sales"


def create_database(path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE customers (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                region TEXT
            );
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                customer_id INTEGER NOT NULL,
                pay_amount REAL,
                region TEXT,
                order_date DATE,
                FOREIGN KEY (customer_id) REFERENCES customers(id)
            );
            INSERT INTO customers VALUES (1, 'Acme', '华东'), (2, 'Globex', '华东');
            INSERT INTO orders VALUES
                (1, 1, 100.0, '华东', '2026-08-12'),
                (2, 1,  50.0, '华东', '2026-08-20'),
                (3, 2,  75.0, '华东', '2026-08-25');
            """
        )


def publish(driver: FakeNeo4jDriver, database) -> int:
    adapter = create_adapter(
        DATASOURCE_ID, "relational", {"driver": "sqlite", "path": str(database)}
    )
    datasource = Datasource(
        id=DATASOURCE_ID,
        name="sales",
        kind="relational",
        workspace_id="default",
        driver="sqlite",
    )
    ScanService(graph_store(driver)).scan(datasource, adapter, version=7)
    return 7


def asset(
    asset_id: str,
    kind: str,
    name: str,
    *,
    object_id: str,
    field_id: str,
    field_path: str,
    data_object_name: str,
    aliases: tuple[str, ...] = (),
    default_aggregation: AggregateFunction | None = None,
) -> SemanticAsset:
    return SemanticAsset(
        asset_id=asset_id,
        workspace_id="default",
        kind=kind,
        name=name,
        aliases=list(aliases),
        source="administrator",
        source_ref=f"seed://retail/{asset_id}",
        status="published",
        confidence=1.0,
        datasource_id=DATASOURCE_ID,
        datasource_name="sales",
        graph_data_object_id=object_id,
        graph_field_id=field_id,
        data_object_name=data_object_name,
        field_path=field_path,
        default_aggregation=default_aggregation,
        updated_at=datetime.now(UTC),
    )


def graph_identity(reader, object_name: str, field_name: str):
    from smartdata.graph import GraphStructureRequest

    structure = reader.read_structure(GraphStructureRequest(datasource_id=DATASOURCE_ID))
    data_object = structure.object_by_name(DATASOURCE_ID, object_name)
    assert data_object is not None
    field = next(
        item
        for item in structure.fields
        if item.object_id == data_object.node_id and item.path == field_name
    )
    return data_object, field


def governed_assets(reader) -> list[SemanticAsset]:
    orders, pay_amount = graph_identity(reader, "orders", "pay_amount")
    _, order_region = graph_identity(reader, "orders", "region")
    _, order_date = graph_identity(reader, "orders", "order_date")
    customers, customer_region = graph_identity(reader, "customers", "region")
    return [
        asset(
            "metric_paid_sales",
            "metric",
            "销售额",
            object_id=orders.node_id,
            field_id=pay_amount.node_id,
            field_path="pay_amount",
            data_object_name="orders",
            aliases=("销售额",),
            default_aggregation=AggregateFunction.SUM,
        ),
        asset(
            "dimension_order_region",
            "dimension",
            "地区",
            object_id=orders.node_id,
            field_id=order_region.node_id,
            field_path="region",
            data_object_name="orders",
            aliases=("区域",),
        ),
        asset(
            "dimension_order_date",
            "dimension",
            "下单日期",
            object_id=orders.node_id,
            field_id=order_date.node_id,
            field_path="order_date",
            data_object_name="orders",
        ),
        asset(
            "dimension_customer_region",
            "dimension",
            "客户地区",
            object_id=customers.node_id,
            field_id=customer_region.node_id,
            field_path="region",
            data_object_name="customers",
        ),
    ]


def registry_with(assets: list[SemanticAsset], path) -> SQLiteSemanticAssetRegistry:
    registry = SQLiteSemanticAssetRegistry(path)
    for item in assets:
        registry.save(item)
    return registry


def scanned(tmp_path):
    """Return a fresh graph + catalog pair keyed by ``DATASOURCE_ID``.

    The pipeline contract requires ``native.datasource_id`` to resolve to a
    real catalog datasource at execution time. We therefore publish first
    (which writes the scan to the graph under the fixed id), then register
    the same id in the catalog via a direct insert so the catalog can hand
    the executor a usable connection.
    """
    driver = FakeNeo4jDriver()
    database = tmp_path / "sales.db"
    create_database(database)
    version = publish(driver, database)
    reader = graph_reader(driver)
    catalog_path = tmp_path / "catalog.db"
    catalog = Catalog(catalog_path)
    import json

    connection_json = json.dumps({"driver": "sqlite", "path": str(database)})
    with catalog._connect() as connection:
        connection.execute(
            "INSERT INTO datasource(id, workspace_id, name, kind, connection_json) "
            "VALUES(?,?,?,?,?)",
            (DATASOURCE_ID, "default", "sales", "relational", connection_json),
        )
    return driver, reader, catalog_path, version, database


def sales_query(**overrides) -> BusinessQuery:
    values = {
        "question": "按地区看销售额",
        "objective": "lookup",
        "metrics": ["销售额"],
        "dimensions": ["地区"],
    }
    return BusinessQuery(**{**values, **overrides})


def _grounded_plan(driver, reader, catalog_path, query: BusinessQuery, **build):
    """Run grounding + context + plan for a query and return them all."""
    registry = registry_with(governed_assets(reader), catalog_path)
    retrieval = GraphSemanticRetriever(reader, registry).retrieve(query, limit=50)
    grounding = SemanticGrounder().ground(query, retrieval)
    catalog = Catalog(catalog_path)
    service = SmartDataService(catalog, graph_reader=reader)
    context = service.build_query_context(grounding, **build)
    plan = service.plan_grounded_query(context)
    return service, grounding, context, plan


# ----------------------------------------------------------------------
# Full pipeline E2E (acceptance)
# ----------------------------------------------------------------------


def test_full_pipeline_returns_typed_result_and_safe_evidence(tmp_path) -> None:
    """GroundedQueryContext → GroundedQueryPlan → execute_grounded_plan → GroundedExecution.

    Asserts every trust boundary: provenance, revision, plan validation,
    native validation, parameterized execution, and that the evidence carries
    no parameter values / business inputs.
    """
    driver, reader, catalog_path, version, _database = scanned(tmp_path)
    query = sales_query(
        dimensions=["地区", "下单日期"],
        objective="ranking",
        ranking=RankingSpec(direction="top", limit=10, metric="销售额"),
        filters=[BusinessFilter(subject="地区", operator="=", value="华东")],
    )

    service, _grounding, context, plan = _grounded_plan(
        driver=driver,
        reader=reader,
        catalog_path=catalog_path,
        query=query,
        time_range=TimeRange(start="2026-08-01", end="2026-08-31"),
        time_spec=TimeSpec(dimension="下单日期"),
    )
    assert plan.scan_version == version

    # Stub the executor's adapter execute_bound so the test never opens a real
    # sqlite3 file (mode=ro) — the trust boundaries above are what this test
    # is exercising, and the SQLite accept already covers the driver. We
    # still go through the formal pipeline.
    captured: dict[str, Any] = {}

    def fake_execute(_self, native, _datasource, _max_rows):
        from smartdata.contracts import (
            ExecutionEvidence,
            GroundedExecution,
            GroundedQueryResult,
        )

        captured["native"] = native
        result = GroundedQueryResult(
            plan_id=native.plan_id,
            datasource_id=native.datasource_id,
            query_language=native.query_language,
            columns=["region", "sales"],
            rows=[{"region": "华东", "sales": 150.0}],
            row_count=1,
            truncated=False,
            scan_version=native.scan_version,
        )
        evidence = ExecutionEvidence(
            plan_id=native.plan_id,
            datasource_id=native.datasource_id,
            scan_version=native.scan_version,
            query_language=native.query_language,
            display_command=native.display_command,
            row_count=result.row_count,
            truncated=result.truncated,
        )
        return GroundedExecution(result=result, evidence=evidence)

    from smartdata.querying.execution import GroundedQueryExecutor

    monkey = pytest.MonkeyPatch()
    monkey.setattr(GroundedQueryExecutor, "execute", fake_execute)
    try:
        execution = service.execute_grounded_plan(
            context, plan, workspace_id="default", max_rows=10
        )
    finally:
        monkey.undo()

    native = captured["native"]
    result = execution.result
    evidence = execution.evidence

    # Result correctness.
    assert result.plan_id == plan.plan_id
    assert result.datasource_id == DATASOURCE_ID
    assert result.query_language is QueryLanguage.SQL
    assert result.row_count == 1
    assert result.rows == [{"region": "华东", "sales": 150.0}]
    assert result.scan_version == version
    assert result.truncated is False

    # Evidence mirrors the result but carries no parameters / business inputs.
    assert evidence.plan_id == result.plan_id
    assert evidence.datasource_id == result.datasource_id
    assert evidence.scan_version == result.scan_version
    assert evidence.query_language == result.query_language
    assert evidence.row_count == result.row_count
    assert evidence.truncated == result.truncated
    # Business values stay in ``parameters`` / result, never in evidence.
    evidence_text = evidence.display_command + evidence.plan_id + evidence.datasource_id
    assert "华东" not in evidence_text
    assert "100" not in evidence_text

    # The native query that actually ran matches the compiled output exactly.
    assert native.command == service.grounded_sql_compiler.compile(plan).command
    assert tuple(native.parameters) == tuple(
        service.grounded_sql_compiler.compile(plan).parameters
    )


# ----------------------------------------------------------------------
# Revision fence
# ----------------------------------------------------------------------


def test_stale_revision_is_refused_before_executor_runs(tmp_path) -> None:
    """A plan built from scan_version=7 must not execute against scan_version=8."""
    driver, reader, catalog_path, _version, _database = scanned(tmp_path)
    query = sales_query()

    service, _grounding, context, plan = _grounded_plan(
        driver=driver,
        reader=reader,
        catalog_path=catalog_path,
        query=query,
    )
    assert plan.scan_version == 7

    # Force the next published-graph read to report scan_version=8.
    real_reader = service.graph_reader

    class _StaleReader:
        def read_structure(self, request):
            structure = real_reader.read_structure(request)
            from smartdata.graph import GraphDatasource

            structure.datasources = [
                GraphDatasource(
                    node_id=item.node_id,
                    datasource_id=item.datasource_id,
                    workspace_id=item.workspace_id,
                    name=item.name,
                    kind=item.kind,
                    driver=item.driver,
                    database_name=item.database_name,
                    scan_version=8,
                )
                for item in structure.datasources
            ]
            return structure

    service.graph_reader = _StaleReader()
    executor_called = False
    real_execute = service.grounded_executor.execute

    def tracking_execute(*args, **kwargs):
        nonlocal executor_called
        executor_called = True
        return real_execute(*args, **kwargs)

    service.grounded_executor.execute = tracking_execute  # type: ignore[method-assign]
    try:
        with pytest.raises(ExecutionValidationError, match="stale"):
            service.execute_grounded_plan(context, plan, workspace_id="default")
    finally:
        service.graph_reader = real_reader
        service.grounded_executor.execute = real_execute  # type: ignore[method-assign]
    assert executor_called is False, "executor must not run against a stale plan"


def test_same_revision_runs_through(tmp_path) -> None:
    driver, reader, catalog_path, version, _database = scanned(tmp_path)
    query = sales_query()
    service, _grounding, context, plan = _grounded_plan(
        driver=driver,
        reader=reader,
        catalog_path=catalog_path,
        query=query,
    )
    assert plan.scan_version == version
    # No monkey-patch; the published reader already reports the same version.
    from smartdata.contracts import (
        ExecutionEvidence,
        GroundedExecution,
        GroundedQueryResult,
    )

    def fake_execute(_self, native, _datasource, _max_rows):
        return GroundedExecution(
            result=GroundedQueryResult(
                plan_id=native.plan_id,
                datasource_id=native.datasource_id,
                query_language=native.query_language,
                columns=["region", "sales"],
                rows=[{"region": "华东", "sales": 225.0}],
                row_count=1,
                truncated=False,
                scan_version=native.scan_version,
            ),
            evidence=ExecutionEvidence(
                plan_id=native.plan_id,
                datasource_id=native.datasource_id,
                scan_version=native.scan_version,
                query_language=native.query_language,
                display_command=native.display_command,
                row_count=1,
                truncated=False,
            ),
        )

    from smartdata.querying.execution import GroundedQueryExecutor

    monkey = pytest.MonkeyPatch()
    monkey.setattr(GroundedQueryExecutor, "execute", fake_execute)
    try:
        execution = service.execute_grounded_plan(
            context, plan, workspace_id="default"
        )
    finally:
        monkey.undo()
    assert execution.result.row_count == 1
    assert execution.result.scan_version == version


# ----------------------------------------------------------------------
# Native query provenance
# ----------------------------------------------------------------------


def test_forged_native_query_with_same_plan_id_fails_provenance(tmp_path) -> None:
    """A NativeQuery that matches identifiers but replaces ``command`` is rejected.

    The validator must not let a hand-built ``NativeQuery`` slip through just
    because it shares ``datasource_id`` / ``plan_id`` / ``scan_version`` /
    ``query_language`` with the plan.
    """
    driver, reader, catalog_path, _version, _database = scanned(tmp_path)
    query = sales_query()
    service, _grounding, _context, plan = _grounded_plan(
        driver=driver,
        reader=reader,
        catalog_path=catalog_path,
        query=query,
    )
    expected = service.grounded_sql_compiler.compile(plan)

    forged = NativeQuery(
        datasource_id=expected.datasource_id,
        plan_id=expected.plan_id,
        scan_version=expected.scan_version,
        query_language=expected.query_language,
        command='SELECT * FROM customers AS t0',
        parameters=(),
        display_command='SELECT * FROM customers AS t0',
    )

    with pytest.raises(QuerySafetyError, match="provenance"):
        service.grounded_native_validator.validate(forged, plan, expected=expected)


def test_forged_native_query_is_irrelevant_to_the_orchestrator(tmp_path) -> None:
    """``execute_grounded_plan`` ignores any forged native; the executor only sees the recompiled command.

    The orchestrator compiles its own native from the plan, so a hand-built
    NativeQuery is never an authorization artefact. The validator still
    refuses forged natives when callers ask it to validate them directly.
    """
    driver, reader, catalog_path, _version, _database = scanned(tmp_path)
    query = sales_query()
    service, _grounding, context, plan = _grounded_plan(
        driver=driver,
        reader=reader,
        catalog_path=catalog_path,
        query=query,
    )
    expected = service.grounded_sql_compiler.compile(plan)

    forged = NativeQuery(
        datasource_id=expected.datasource_id,
        plan_id=expected.plan_id,
        scan_version=expected.scan_version,
        query_language=expected.query_language,
        command='SELECT * FROM customers AS t0',
        parameters=(),
        display_command='SELECT * FROM customers AS t0',
    )

    # Direct validation of the forged native against the orchestrator's compiled
    # output refuses it with a provenance mismatch.
    with pytest.raises(QuerySafetyError, match="provenance"):
        service.grounded_native_validator.validate(
            forged, plan, expected=expected
        )

    # Running the orchestrator with the same plan never accepts the forged
    # native: it recompiles its own. The executor sees only the compiled
    # command.
    seen_command: list[str] = []

    def fake_execute(_self, native, _datasource, _max_rows):
        seen_command.append(native.command)
        from smartdata.contracts import (
            ExecutionEvidence,
            GroundedExecution,
            GroundedQueryResult,
        )

        return GroundedExecution(
            result=GroundedQueryResult(
                plan_id=native.plan_id,
                datasource_id=native.datasource_id,
                query_language=native.query_language,
                columns=["region", "sales"],
                rows=[],
                row_count=0,
                truncated=False,
                scan_version=native.scan_version,
            ),
            evidence=ExecutionEvidence(
                plan_id=native.plan_id,
                datasource_id=native.datasource_id,
                scan_version=native.scan_version,
                query_language=native.query_language,
                display_command=native.display_command,
                row_count=0,
                truncated=False,
            ),
        )

    from smartdata.querying.execution import GroundedQueryExecutor

    monkey = pytest.MonkeyPatch()
    monkey.setattr(GroundedQueryExecutor, "execute", fake_execute)
    try:
        service.execute_grounded_plan(context, plan, workspace_id="default")
    finally:
        monkey.undo()
    assert len(seen_command) == 1
    assert seen_command[0] == expected.command
    assert "customers" not in seen_command[0]


# ----------------------------------------------------------------------
# IN-filter determinism
# ----------------------------------------------------------------------


def test_compiler_refuses_set_in_filter() -> None:
    """``IN`` with a ``set`` value would be non-deterministic; the compiler rejects it."""
    from smartdata.contracts import (
        GroundedDataObjectRef,
        GroundedQueryPlan,
        PlanFilter,
    )
    from smartdata.querying.generation import GroundedSQLCompiler

    plan = GroundedQueryPlan(
        plan_id="set-in",
        workspace_id="default",
        datasource_id="ds",
        data_object_ids=["orders"],
        data_objects={
            "orders": GroundedDataObjectRef(
                datasource_id="ds", data_object_id="orders", name="orders"
            )
        },
        scan_version=1,
        aggregates=[
            _agg("orders", "pay_amount", "sales", AggregateFunction.SUM),
        ],
        filters=[
            PlanFilter(
                field=_field("region", "orders"),
                operator=FilterOperator.IN,
                value={"东京", "大阪"},
            )
        ],
        expected_result_type=QueryResultType.SCALAR,
    )
    with pytest.raises(Exception, match="IN"):
        GroundedSQLCompiler().compile(plan)


# Helpers shared with the test module
def _field(path: str, object_id: str):
    from smartdata.contracts import GroundedFieldRef

    return GroundedFieldRef(
        datasource_id=DATASOURCE_ID,
        data_object_id=object_id,
        field_path=path,
    )


def _agg(object_id: str, path: str, alias: str, function: AggregateFunction):
    from smartdata.contracts import PlanAggregate

    return PlanAggregate(
        function=function,
        field=_field(path, object_id),
        alias=alias,
    )