"""MCP tool behavior against a real service (RS-MCP-01).

These drive the tool functions directly rather than through a client session, which is what makes
them useful for pinning argument handling and return shape. Protocol-level behavior - discovery,
progress, error flags - is covered by ``test_mcp_protocol.py`` over real stdio.

The tools are async because ``ask_data`` consumes ``ask_stream`` once and reports progress from it;
a synchronous tool could not do that without blocking the event loop.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import qaneris.interfaces.mcp.server as mcp_server
from qaneris.application.service import QanerisService
from qaneris.catalog import Catalog
from qaneris.contracts import MappingInfo, RelationInfo
from qaneris.contracts.connection import SecureDatasourceCreate


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def service(tmp_path: Path, monkeypatch) -> QanerisService:
    instance = QanerisService(Catalog(tmp_path / "catalog.db"))
    monkeypatch.setattr(mcp_server, "service", instance)
    return instance


def source_database(path: Path) -> Path:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, amount REAL NOT NULL)")
        connection.execute("INSERT INTO orders VALUES (1, 10.5), (2, 20.0)")
    return path


@pytest.mark.anyio
async def test_lists_relations_and_mappings(service: QanerisService, tmp_path: Path) -> None:
    source = source_database(tmp_path / "sales.db")
    datasource = service.create_secure_datasource(
        SecureDatasourceCreate(
            name="sales",
            kind="relational",
            connection_profile={"driver": "sqlite", "endpoint": {"path": str(source)}},
        )
    )
    relation = RelationInfo(
        datasource_id=datasource.id,
        from_dataset="orders",
        to_dataset="customers",
        relation_type="foreign_key",
    )
    service.catalog.replace_relations(datasource.id, [relation])
    mapping = service.catalog.create_mapping(
        MappingInfo(
            entity="customer",
            canonical_field="customer_id",
            datasource_id=datasource.id,
            dataset="customers",
            field="id",
        )
    )

    assert json.loads(await mcp_server.list_relations()) == [relation.model_dump(mode="json")]
    assert json.loads(await mcp_server.list_mappings(entity="customer")) == [
        mapping.model_dump(mode="json")
    ]


@pytest.mark.anyio
async def test_explicit_read_only_sql_remains_available(
    service: QanerisService, tmp_path: Path
) -> None:
    """The optional ``sql`` argument still goes through read-only validation."""
    source = source_database(tmp_path / "sales.db")
    datasource = service.create_secure_datasource(
        SecureDatasourceCreate(
            name="sales",
            kind="relational",
            connection_profile={"driver": "sqlite", "endpoint": {"path": str(source)}},
        )
    )
    service.scan_datasource(datasource.id)

    response = json.loads(
        await mcp_server.ask_data(
            "orders amount 总额",
            datasource_id=datasource.id,
            sql='SELECT SUM("amount") AS "total_amount" FROM "orders"',
        )
    )

    assert response["result"]["rows"] == [{"total_amount": 30.5}]
    assert response["plan"][0]["query_language"] == "sql"


@pytest.mark.anyio
async def test_explicit_sql_cannot_bypass_the_safety_check(
    service: QanerisService, tmp_path: Path
) -> None:
    source = source_database(tmp_path / "sales.db")
    datasource = service.create_secure_datasource(
        SecureDatasourceCreate(
            name="sales",
            kind="relational",
            connection_profile={"driver": "sqlite", "endpoint": {"path": str(source)}},
        )
    )
    service.scan_datasource(datasource.id)

    with pytest.raises(RuntimeError) as raised:
        await mcp_server.ask_data(
            "删除订单", datasource_id=datasource.id, sql="DELETE FROM orders"
        )

    assert "unsafe_query" in str(raised.value)


@pytest.mark.anyio
async def test_schema_tools_answer_without_a_catalog_handle(
    service: QanerisService, tmp_path: Path
) -> None:
    source = source_database(tmp_path / "sales.db")
    datasource = service.create_secure_datasource(
        SecureDatasourceCreate(
            name="sales",
            kind="relational",
            connection_profile={"driver": "sqlite", "endpoint": {"path": str(source)}},
        )
    )

    context = json.loads(await mcp_server.get_schema_context())
    assert [item["id"] for item in context["datasources"]] == [datasource.id]

    adapters = json.loads(await mcp_server.list_adapters())
    assert any(item["driver"] == "sqlite" for item in adapters)

    assert json.loads(await mcp_server.list_datasources()) != []
    assert isinstance(await mcp_server.summarize_schema(), str)


@pytest.mark.anyio
async def test_search_dataset_finds_by_name_and_field(
    service: QanerisService, tmp_path: Path
) -> None:
    source = source_database(tmp_path / "sales.db")
    datasource = service.create_secure_datasource(
        SecureDatasourceCreate(
            name="sales",
            kind="relational",
            connection_profile={"driver": "sqlite", "endpoint": {"path": str(source)}},
        )
    )
    service.scan_datasource(datasource.id)

    assert [item["name"] for item in json.loads(await mcp_server.search_dataset("orders"))] == [
        "orders"
    ]
    assert [item["name"] for item in json.loads(await mcp_server.search_dataset("amount"))] == [
        "orders"
    ]
    assert json.loads(await mcp_server.search_dataset("no_such_dataset")) == []
