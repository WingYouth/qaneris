from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import Mock

import pytest

from smartdata.adapters.document.mongodb import MongoDBAdapter
from smartdata.adapters.key_value.redis import RedisAdapter
from smartdata.adapters.relational.sqlalchemy import SQLAlchemyAdapter
from smartdata.application.service import SmartDataService
from smartdata.common.errors import QueryPlanningError, QuerySafetyError
from smartdata.contracts import (
    AggregateFunction,
    Datasource,
    DatasourceKind,
    FilterOperator,
    GroundedDataObjectRef,
    GroundedFieldRef,
    GroundedQueryContext,
    GroundedQueryPlan,
    PlanAggregate,
    PlanFilter,
    PlanJoin,
    PlanSort,
    PlanSortTarget,
    QueryContextBinding,
    QueryContextFilter,
    QueryResultType,
    SortDirection,
    TimeRange,
)
from smartdata.querying.generation.native import GroundedNativeCompiler
from smartdata.querying.planning.grounded_planner import GroundedQueryPlanner
from smartdata.querying.validation.native import GroundedNativeQueryValidator, _placeholder_count
from smartdata.querying.validation.plans import GroundedPlanValidator
from smartdata.contracts.semantic import BusinessObjective, BusinessQuery, SemanticAssetType


def source(driver: str) -> Datasource:
    kind = (DatasourceKind.DOCUMENT if driver == "mongodb" else
            DatasourceKind.KEY_VALUE if driver == "redis" else DatasourceKind.RELATIONAL)
    return Datasource(id="ds", name="source", workspace_id="default", kind=kind, driver=driver)


def plan(driver: str, **overrides) -> GroundedQueryPlan:
    kind = "string" if driver == "redis" else "collection" if driver == "mongodb" else "table"
    name = "redis:string" if driver == "redis" else "orders"
    field = "value" if driver == "redis" else "amount"
    values = {"plan_id": "p", "workspace_id": "default", "datasource_id": "ds",
              "data_object_ids": ["obj"], "data_objects": {"obj": GroundedDataObjectRef(
                  datasource_id="ds", data_object_id="obj", name=name, object_kind=kind)},
              "scan_version": 7, "selected_fields": [ref(field)],
              "expected_result_type": QueryResultType.TABULAR, "limit": 20}
    values.update(overrides)
    return GroundedQueryPlan(**values)


def ref(name: str) -> GroundedFieldRef:
    return GroundedFieldRef(datasource_id="ds", data_object_id="obj", field_path=name)


@pytest.mark.parametrize("driver,quote,placeholder", [
    ("sqlite", '"', "?"), ("postgresql", '"', "%s"), ("mysql", "`", "%s")])
def test_sql_dialects_bind_all_business_values(driver, quote, placeholder):
    p = plan(driver, selected_fields=[ref("region")], group_by=[ref("region")],
             aggregates=[PlanAggregate(function=AggregateFunction.SUM, field=ref("amount"), alias="sales")],
             filters=[PlanFilter(field=ref("region"), operator=FilterOperator.EQUALS,
                                 value="' OR 1=1 --")],
             time_field=ref("ordered_at"),
             time_range=TimeRange(start="2026-01-01", end="2026-01-31"),
             sorts=[PlanSort(target=PlanSortTarget.AGGREGATE, aggregate_alias="sales",
                             direction=SortDirection.DESCENDING)])
    native = GroundedNativeCompiler().compile(p, source(driver))
    GroundedNativeQueryValidator().validate(native, p, expected=native)
    assert f"{quote}orders{quote}" in native.command
    assert _placeholder_count(native.command, driver) == 4
    assert native.parameters == ("' OR 1=1 --", "2026-01-01", "2026-01-31", 20)
    assert "1=1" not in native.command + native.display_command
    assert f"LIMIT {placeholder}" in native.command


@pytest.mark.parametrize("driver", ["sqlite", "postgresql", "mysql"])
def test_sql_placeholder_scanner_ignores_literals_comments_and_identifiers(driver):
    placeholder = "?" if driver == "sqlite" else "%s"
    query = f'SELECT {placeholder}, \'{placeholder}\', "{placeholder}", `{placeholder}` /* {placeholder} */'
    assert _placeholder_count(query, driver) == 1
    wrong = "%s" if driver == "sqlite" else "?"
    with pytest.raises(QuerySafetyError, match="dialect"):
        _placeholder_count(f"SELECT {wrong}", driver)


@pytest.mark.parametrize("operator,value", [
    (FilterOperator.EQUALS, None), (FilterOperator.IN, [1, 2]),
    (FilterOperator.BETWEEN, [1, 2]), (FilterOperator.CONTAINS, "a%b")])
def test_sql_filter_forms_for_each_dialect(operator, value):
    for driver in ("sqlite", "postgresql", "mysql"):
        p = plan(driver, filters=[PlanFilter(field=ref("amount"), operator=operator, value=value)])
        native = GroundedNativeCompiler().compile(p, source(driver))
        GroundedNativeQueryValidator().validate(native, p, expected=native)
        assert "a%b" not in native.command


@pytest.mark.parametrize("driver,quote", [("sqlite", '"'), ("postgresql", '"'),
                                          ("mysql", "`")])
def test_sql_confirmed_direct_join(driver, quote):
    p = plan(driver, data_object_ids=["obj", "customer"],
             data_objects={"obj": GroundedDataObjectRef(
                 datasource_id="ds", data_object_id="obj", name="orders", object_kind="table"),
                           "customer": GroundedDataObjectRef(
                               datasource_id="ds", data_object_id="customer", name="customers",
                               object_kind="table")},
             joins=[PlanJoin(datasource_id="ds", relationship_id="confirmed",
                             from_data_object_id="obj", from_field_path="customer_id",
                             to_data_object_id="customer", to_field_path="id")])
    native = GroundedNativeCompiler().compile(p, source(driver))
    GroundedNativeQueryValidator().validate(native, p, expected=native)
    assert f"JOIN {quote}customers{quote}" in native.command
    assert f"t0.{quote}customer_id{quote} = t1.{quote}id{quote}" in native.command


def test_formal_dispatch_rejects_other_registered_driver():
    ds = source("sqlite").model_copy(update={"driver": "oracle"})
    with pytest.raises(QueryPlanningError, match="not enabled"):
        GroundedNativeCompiler().compile(plan("sqlite"), ds)


@pytest.mark.parametrize("driver,field", [("sqlite", "amount"),
                                          ("mongodb", "region"), ("redis", "value")])
def test_public_query_event_omits_native_values(driver, field):
    secret = "private-business-value"
    filter_field = "key" if driver == "redis" else field
    p = plan(driver, selected_fields=[ref(field)], filters=[PlanFilter(
        field=ref(filter_field), operator=FilterOperator.EQUALS, value=secret)])
    native = GroundedNativeCompiler().compile(p, source(driver))
    payload = SmartDataService._native_query_payload(native)
    assert set(payload) == {"stage", "datasource_id", "scan_version", "plan_id",
                            "query_language", "display_command"}
    assert secret not in str(payload)


@pytest.mark.parametrize("driver,projected,operation", [
    ("mongodb", "region", "find"), ("redis", "value", "GET")])
def test_lookup_context_plans_a_native_projection(driver, projected, operation):
    ds, example = (source(driver), plan(driver))
    locator = example.data_objects["obj"]
    dimension = QueryContextBinding(
        business_term=projected, asset_type=SemanticAssetType.DIMENSION,
        asset_id=f"dimension-{projected}", datasource_id="ds", data_object_id="obj",
        field_path=projected, score=1.0, object_kind=locator.object_kind,
        data_object_name=locator.name)
    bindings = [dimension]
    filters = []
    allowed = [projected]
    if driver == "redis":
        key = QueryContextBinding(
            business_term="key", asset_type=SemanticAssetType.FIELD,
            asset_id="field-key", datasource_id="ds", data_object_id="obj",
            field_path="key", score=1.0, object_kind=locator.object_kind,
            data_object_name=locator.name)
        bindings.append(key)
        filters = [QueryContextFilter(subject="key", field=key.field_ref(),
                                      operator=FilterOperator.EQUALS, value="secret-key")]
        allowed.append("key")
    context = GroundedQueryContext(
        workspace_id="default", objective=BusinessObjective.LOOKUP,
        business_query=BusinessQuery(question="lookup", objective=BusinessObjective.LOOKUP,
                                     dimensions=[projected]),
        bindings=bindings, dimensions=[dimension], filters=filters,
        data_objects={"obj": locator}, scan_version=7,
        allowed_datasource_ids=["ds"], allowed_data_object_ids=["obj"],
        allowed_field_paths=allowed)
    planned = GroundedQueryPlanner().plan(context)
    GroundedPlanValidator().validate(planned, context)
    assert not planned.aggregates and not planned.group_by
    native = GroundedNativeCompiler().compile(planned, ds)
    GroundedNativeQueryValidator().validate(native, planned, expected=native)
    if driver == "mongodb":
        assert native.command["operation"] == operation
    else:
        assert native.command["command"] == operation


def test_mongo_find_aggregate_and_provenance():
    secret = "private.*?"
    p = plan("mongodb", selected_fields=[ref("region")],
             filters=[PlanFilter(field=ref("region"), operator=FilterOperator.CONTAINS,
                                 value=secret)],
             sorts=[PlanSort(target=PlanSortTarget.FIELD, field=ref("region"))])
    compiler = GroundedNativeCompiler()
    validator = GroundedNativeQueryValidator()
    native = compiler.compile(p, source("mongodb"))
    validator.validate(native, p, expected=native)
    assert native.command["operation"] == "find"
    assert native.command["filter"]["region"]["$regex"] == r"private\.\*\?"
    assert secret not in native.display_command
    forged = native.model_copy(update={"command": {**native.command, "limit": 100}})
    with pytest.raises(QuerySafetyError):
        validator.validate(forged, p, expected=native)
    p = plan("mongodb", selected_fields=[ref("region")], group_by=[ref("region")],
             aggregates=[PlanAggregate(function=fn, field=None if fn is AggregateFunction.COUNT else ref("amount"), alias=fn.value)
                         for fn in AggregateFunction],
             time_field=ref("ordered_at"), time_range=TimeRange(start="2026-01-01"))
    native = compiler.compile(p, source("mongodb"))
    validator.validate(native, p, expected=native)
    assert native.command["operation"] == "aggregate"
    assert [next(iter(stage)) for stage in native.command["pipeline"]] == [
        "$match", "$group", "$project", "$limit"]
    assert "$lookup" not in str(native.command)
    assert "$out" not in str(native.command)
    assert "$merge" not in str(native.command)
    assert "2026-01-01" not in native.display_command
    with pytest.raises(QuerySafetyError):
        validator.validate(native.model_copy(update={"command": {
            **native.command, "pipeline": [{"$out": "orders"}]}}), p, expected=native)


@pytest.mark.parametrize("kind,field,filter_value,command", [
    ("string", "value", "x", "GET"), ("string", "value", ["x", "y"], "MGET"),
    ("hash", "value", "x", "HGETALL"), ("list", "value", "x", "LRANGE"),
    ("set", "value", "x", "SMEMBERS"), ("zset", "value", "x", "ZRANGE"),
    ("string", "key", None, "SCAN"), ("string", "exists", "x", "EXISTS"),
    ("string", "type", "x", "TYPE"), ("string", "ttl", "x", "TTL"),
    ("set", "cardinality", "x", "SCARD"), ("zset", "cardinality", "x", "ZCARD")])
def test_redis_mapping(kind, field, filter_value, command):
    filters = [] if filter_value is None else [PlanFilter(
        field=ref("key"), operator=FilterOperator.IN if isinstance(filter_value, list) else FilterOperator.EQUALS,
        value=filter_value)]
    p = plan("redis", selected_fields=[ref(field)], filters=filters,
             data_objects={"obj": GroundedDataObjectRef(
                 datasource_id="ds", data_object_id="obj", name=f"redis:{kind}", object_kind=kind)})
    native = GroundedNativeCompiler().compile(p, source("redis"))
    GroundedNativeQueryValidator().validate(native, p, expected=native)
    assert native.command["command"] == command
    assert "\"x\"" not in native.display_command
    with pytest.raises(QuerySafetyError):
        GroundedNativeQueryValidator().validate(native.model_copy(update={"command": {
            "command": "SET", "args": ["x", "y"]}}), p, expected=native)


def test_redis_unsupported_relational_operations_fail_closed():
    with pytest.raises(QueryPlanningError):
        GroundedNativeCompiler().compile(plan("redis", selected_fields=[ref("value")],
                                               group_by=[ref("value")]), source("redis"))
    with pytest.raises(QueryPlanningError):
        GroundedNativeCompiler().compile(plan("redis", selected_fields=[ref("value")],
                                               aggregates=[PlanAggregate(function=AggregateFunction.COUNT, alias="count")]),
                                         source("redis"))


def test_sqlalchemy_formal_binding_and_truncation(monkeypatch):
    adapter = SQLAlchemyAdapter("ds", {"driver": "postgresql", "url": "postgresql+psycopg://invalid"})
    connection = Mock()
    result = Mock()
    result.fetchmany.return_value = [Mock(_mapping={"x": 1}), Mock(_mapping={"x": 2})]
    result.keys.return_value = ["x"]
    connection.exec_driver_sql.return_value = result
    @contextmanager
    def connect():
        yield connection
    monkeypatch.setattr(adapter, "_connect", connect)
    output = adapter.execute_bound("SELECT x FROM t WHERE x = %s", (1,), 1)
    connection.exec_driver_sql.assert_called_once_with("SELECT x FROM t WHERE x = %s", (1,))
    result.fetchmany.assert_called_once_with(2)
    assert output.rows == [{"x": 1}] and output.truncated
    with pytest.raises(QuerySafetyError):
        adapter.execute_bound("DELETE FROM t", (), 1)


def test_mongo_typed_adapter_and_redis_scan_are_bounded(monkeypatch):
    mongo = MongoDBAdapter("ds", {"database": "test"})
    cursor = Mock()
    cursor.limit.return_value = [{"x": 1}, {"x": 2}]
    collection = Mock()
    collection.find.return_value = cursor
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=None)
    client.__getitem__ = Mock(return_value={"orders": collection})
    monkeypatch.setattr(mongo, "_client", lambda: client)
    result = mongo.execute_native({"operation": "find", "collection": "orders", "filter": {},
                                   "projection": {"x": 1}, "sort": [], "limit": 20}, max_rows=1)
    assert result.row_count == 1 and result.truncated
    redis = RedisAdapter("ds", {})
    client = Mock()
    client.scan.side_effect = [(1, ["a", "b"]), (0, ["c"])]
    client.type.return_value = "string"
    monkeypatch.setattr(redis, "_client", lambda: client)
    result = redis.execute_native({"command": "SCAN", "args": [], "kind": "string", "limit": 2},
                                  max_rows=2)
    assert result.columns == ["key"]
    assert result.rows == [{"key": "a"}, {"key": "b"}]
