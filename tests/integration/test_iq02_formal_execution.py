"""Formal plan-to-adapter tests with local/fake database backends, never real-server claims."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from smartdata.adapters.document.mongodb import MongoDBAdapter
from smartdata.adapters.key_value.redis import RedisAdapter
from smartdata.adapters.relational.sqlalchemy import SQLAlchemyAdapter
from smartdata.application.service import SmartDataService
from smartdata.catalog import Catalog
from smartdata.common.errors import ExecutionValidationError
from smartdata.contracts import (
    AggregateFunction,
    Datasource,
    DatasourceKind,
    FilterOperator,
    GroundedDataObjectRef,
    GroundedFieldRef,
    GroundedQueryPlan,
    PlanAggregate,
    PlanFilter,
    QueryResultType,
)
from smartdata.querying.execution import GroundedQueryExecutor
from smartdata.querying.generation.native import GroundedNativeCompiler
from smartdata.querying.validation.native import GroundedNativeQueryValidator


def case(driver: str, *, key: str = "secret-key") -> tuple[Datasource, GroundedQueryPlan]:
    kind = (DatasourceKind.DOCUMENT if driver == "mongodb" else
            DatasourceKind.KEY_VALUE if driver == "redis" else DatasourceKind.RELATIONAL)
    object_kind = "string" if driver == "redis" else "collection" if driver == "mongodb" else "table"
    object_name = "redis:string" if driver == "redis" else "orders"
    selected = "value" if driver == "redis" else "region" if driver == "mongodb" else "amount"
    filters = [PlanFilter(field=GroundedFieldRef(
        datasource_id="ds", data_object_id="obj", field_path="key" if driver == "redis" else "region"),
        operator=FilterOperator.EQUALS, value=key)] if driver in {"redis", "mongodb"} else []
    ds = Datasource(id="ds", name="test", kind=kind, driver=driver, workspace_id="default")
    plan = GroundedQueryPlan(plan_id="p", workspace_id="default", datasource_id="ds",
                             data_object_ids=["obj"], data_objects={"obj": GroundedDataObjectRef(
                                 datasource_id="ds", data_object_id="obj", name=object_name,
                                 object_kind=object_kind)}, scan_version=7,
                             selected_fields=[GroundedFieldRef(
                                 datasource_id="ds", data_object_id="obj", field_path=selected)],
                             filters=filters, expected_result_type=QueryResultType.TABULAR, limit=2)
    return ds, plan


class Connections:
    def __init__(self, config):
        self.config = config
        self.opened = 0

    @contextmanager
    def open(self, datasource_id):
        self.opened += 1
        yield self.config


@pytest.mark.parametrize("driver", ["sqlite", "postgresql", "mysql", "mongodb", "redis"])
def test_formal_compiler_validator_executor_adapter(driver, tmp_path, monkeypatch):
    ds, plan = case(driver)
    native = GroundedNativeCompiler().compile(plan, ds)
    GroundedNativeQueryValidator().validate(native, plan, expected=native)
    config = {"driver": driver}
    if driver == "sqlite":
        database = tmp_path / "source.db"
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE orders (amount INTEGER)")
            connection.execute("INSERT INTO orders VALUES (42)")
        config["path"] = str(database)
    elif driver in {"postgresql", "mysql"}:
        config["url"] = "unused"
        result = Mock()
        result.keys.return_value = ["amount"]
        result.fetchmany.return_value = [Mock(_mapping={"amount": 42})]
        connection = Mock()
        connection.exec_driver_sql.return_value = result

        @contextmanager
        def connected(_self):
            yield connection

        monkeypatch.setattr(SQLAlchemyAdapter, "_connect", connected)
    elif driver == "mongodb":
        cursor = Mock()
        cursor.limit.return_value = [{"region": "east"}]
        collection = Mock()
        collection.find.return_value = cursor
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=None)
        client.__getitem__ = Mock(return_value={"orders": collection})
        config["database"] = "test"
        monkeypatch.setattr(MongoDBAdapter, "_client", lambda _self: client)
    else:
        client = Mock()
        client.get.return_value = "value"
        monkeypatch.setattr(RedisAdapter, "_client", lambda _self: client)
    connections = Connections(config)
    execution = GroundedQueryExecutor(connections).execute(native, ds, max_rows=2)
    assert connections.opened == 1
    assert execution.result.row_count == 1
    assert execution.evidence.display_command == native.display_command
    assert "secret-key" not in execution.evidence.model_dump_json()
    if driver in {"postgresql", "mysql"}:
        connection.exec_driver_sql.assert_called_once_with(native.command, native.parameters)


@pytest.mark.parametrize("driver", ["sqlite", "postgresql", "mysql", "mongodb", "redis"])
def test_stale_version_fails_before_source_connection(driver, tmp_path):
    ds, plan = case(driver)
    service = SmartDataService(Catalog(tmp_path / "catalog.db"))
    service.catalog.get_datasource = Mock(return_value=(ds, {}))
    service.grounded_plan_validator.validate = Mock()
    service.graph_reader.read_structure = Mock(return_value=SimpleNamespace(datasources=[
        SimpleNamespace(datasource_id="ds", scan_version=8)]))
    connections = Connections({"driver": driver})
    service.grounded_executor.connections = connections
    with pytest.raises(ExecutionValidationError, match="stale"):
        service.execute_grounded_plan(object(), plan, workspace_id="default")
    assert connections.opened == 0


def test_formal_mongo_aggregate_reaches_typed_adapter(monkeypatch):
    ds, base = case("mongodb")
    plan = base.model_copy(update={
        "aggregates": [PlanAggregate(function=AggregateFunction.COUNT, alias="count_rows")],
        "selected_fields": [], "filters": [],
    })
    native = GroundedNativeCompiler().compile(plan, ds)
    GroundedNativeQueryValidator().validate(native, plan, expected=native)
    collection = Mock()
    collection.aggregate.return_value = iter([{"count_rows": 1000}])
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=None)
    client.__getitem__ = Mock(return_value={"orders": collection})
    monkeypatch.setattr(MongoDBAdapter, "_client", lambda _self: client)
    result = GroundedQueryExecutor(Connections({"driver": "mongodb", "database": "test"})).execute(
        native, ds).result
    assert result.rows == [{"count_rows": 1000}]
    collection.aggregate.assert_called_once_with(native.command["pipeline"])


def test_formal_redis_bounded_scan_reaches_typed_adapter(monkeypatch):
    ds, base = case("redis")
    plan = base.model_copy(update={"selected_fields": [GroundedFieldRef(
        datasource_id="ds", data_object_id="obj", field_path="key")], "filters": []})
    native = GroundedNativeCompiler().compile(plan, ds)
    GroundedNativeQueryValidator().validate(native, plan, expected=native)
    client = Mock()
    client.scan.return_value = (1, ["secret-key", "other-key", "third-key"])
    client.type.return_value = "string"
    monkeypatch.setattr(RedisAdapter, "_client", lambda _self: client)
    execution = GroundedQueryExecutor(Connections({"driver": "redis"})).execute(native, ds, max_rows=2)
    assert execution.result.columns == ["key"]
    assert execution.result.rows == [{"key": "secret-key"}, {"key": "other-key"}]
    assert execution.result.truncated
    assert "secret-key" not in execution.evidence.display_command
