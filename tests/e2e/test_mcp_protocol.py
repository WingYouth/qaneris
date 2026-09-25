"""Real stdio MCP protocol acceptance (RS-MCP-01).

A real server process is spawned and driven by a real MCP client over stdio - no in-process
shortcut, no substituted service - and it walks the Roadshow path end to end:

    initialize → list_tools → register_sqlite → scan_datasource → ask_data (with progress)

Two things are checked that can only be checked here. First, that the tools a client discovers are
the product's tools, with schemas that cannot carry a secret. Second, that ``ask_data`` reports
progress the way a client actually receives it: one notification per stage, ascending, ``done``
last, and none of it fabricated by the transport.

The natural-language question contract needs a model gateway and a published Neo4j graph. When
those are absent the test **skips with the reason**, rather than being softened into something that
passes without them: a green run must mean the path was exercised, not that it was avoided.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from examples.create_demo_database import create_demo_database

PROJECT_ROOT = Path(__file__).parents[2]

#: Applied to every tool schema: a caller may name a stored secret, never supply one.
RAW_SECRET_FIELDS = {
    "password",
    "token",
    "api_key",
    "secret",
    "private_key_password",
    "certificate_pem",
    "private_key_pem",
    "connection_json",
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def server_parameters(tmp_path: Path) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "qaneris.interfaces.mcp.server"],
        env={
            "QANERIS_CATALOG": str(tmp_path / "catalog.db"),
            # The null store keeps publication a no-op. The Scan path is still real; only the graph
            # backend is absent, which is why the natural-language test below is the one that needs
            # to be gated on a real graph.
            "QANERIS_GRAPH_STORE": "null",
        },
        cwd=PROJECT_ROOT,
    )


def graph_backend_ready() -> bool:
    """Whether a published graph could actually be read in this environment.

    A configured-but-unreachable Neo4j is not "ready": the semantic retrieval step would fail on
    connect, so the check is a real connection attempt rather than an environment variable test.
    """
    if os.environ.get("QANERIS_GRAPH_STORE", "neo4j").strip().lower() != "neo4j":
        return False
    uri = os.environ.get("QANERIS_NEO4J_URI")
    if not uri:
        return False
    parsed = urlsplit(uri)
    host = parsed.hostname or "localhost"
    port = parsed.port or 7687
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def model_gateway_ready() -> bool:
    from qaneris.llm.gateway import AiyallmSchemaModel

    return AiyallmSchemaModel.from_environment() is not None


def collect_schema_fields(schema: dict, prefix: str = "") -> list[tuple[str, dict]]:
    found: list[tuple[str, dict]] = []
    if isinstance(schema, dict):
        for name, child in schema.get("properties", {}).items():
            found.append((f"{prefix}/{name}", child))
            found.extend(collect_schema_fields(child, f"{prefix}/{name}"))
        for name, child in schema.get("$defs", {}).items():
            found.extend(collect_schema_fields(child, f"%defs:{name}"))
        for key in ("anyOf", "allOf", "oneOf", "items"):
            child = schema.get(key)
            if isinstance(child, list):
                for item in child:
                    found.extend(collect_schema_fields(item, prefix))
            elif isinstance(child, dict):
                found.extend(collect_schema_fields(child, prefix))
    return found


async def read_demo_rows(session: ClientSession, datasource_id: str, question: str) -> dict:
    return json.loads(
        (await session.call_tool("ask_data", {"question": question, "datasource_id": datasource_id}))
        .content[0].text
    )


@pytest.mark.anyio
async def test_stdio_roadshow_path_with_progress(
    tmp_path: Path, server_parameters: StdioServerParameters
) -> None:
    """The full Roadshow path over real stdio, with a client that receives progress."""
    demo_database = create_demo_database(tmp_path / "sales.db")
    progress: list[tuple[float, float | None, str | None]] = []

    async def on_progress(value: float, total: float | None, message: str | None) -> None:
        progress.append((value, total, message))

    async with (
        stdio_client(server_parameters) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        initialized = await session.initialize()
        tools = (await session.list_tools()).tools
        tool_names = {tool.name for tool in tools}
        schemas = {tool.name: tool.inputSchema for tool in tools}

        listing = await session.call_tool("list_datasources", {"workspace_id": "default"})

        registration = await session.call_tool(
            "register_sqlite",
            {"name": "sales", "path": str(demo_database), "workspace_id": "default"},
        )
        datasource = json.loads(registration.content[0].text)

        scanned = await session.call_tool("scan_datasource", {"datasource_id": datasource["id"]})
        detail = await session.call_tool("inspect_datasource", {"datasource_id": datasource["id"]})

        answered = await session.call_tool(
            "ask_data",
            {
                "question": "orders 的总金额是多少？",
                "datasource_id": datasource["id"],
                "sql": 'SELECT SUM("total_amount") AS "total" FROM "orders"',
            },
            progress_callback=on_progress,
        )
        # The same question again, with a client that cannot receive progress.
        quiet = await session.call_tool(
            "ask_data",
            {
                "question": "orders 的总金额是多少？",
                "datasource_id": datasource["id"],
                "sql": 'SELECT SUM("total_amount") AS "total" FROM "orders"',
            },
        )
        unsafe = await session.call_tool(
            "ask_data",
            {
                "question": "删除订单",
                "datasource_id": datasource["id"],
                "sql": "DELETE FROM orders",
            },
        )

    # -- the server and its surface ------------------------------------------------------------
    assert initialized.serverInfo.name == "Qaneris"
    assert {
        "list_adapters",
        "list_datasources",
        "inspect_datasource",
        "test_secure_datasource",
        "create_secure_datasource",
        "update_secure_datasource",
        "delete_datasource",
        "register_sqlite",
        "scan_datasource",
        "search_dataset",
        "ask_data",
    }.issubset(tool_names)
    # The legacy raw-JSON registration bridge is gone from the real tool list.
    assert "register_datasource" not in tool_names

    # No discovered schema can carry a secret value.
    for tool_name, schema in schemas.items():
        for path, subschema in collect_schema_fields(schema):
            leaf = path.rsplit("/", 1)[-1]
            assert leaf not in RAW_SECRET_FIELDS or "SecretReference" in json.dumps(subschema), (
                f"{tool_name}{path} could carry a raw secret"
            )

    # -- datasource lifecycle ------------------------------------------------------------------
    assert not listing.isError
    assert json.loads(listing.content[0].text) == []
    assert not registration.isError
    # Test → Save → Scan: registration saves but does not scan.
    assert datasource["status"] == "created"
    assert not scanned.isError
    assert json.loads(detail.content[0].text)["status"] == "ready"
    assert json.loads(detail.content[0].text)["scan_version"] is not None

    # -- ask and progress ----------------------------------------------------------------------
    assert not answered.isError
    messages = [item[2] for item in progress]
    assert messages[0] == "accepted"
    assert messages[-1] == "done"
    assert "result_ready" in messages
    assert messages.index("result_ready") < messages.index("done")
    values = [item[0] for item in progress]
    assert values == sorted(values) and len(set(values)) == len(values)
    # No fabricated total: the number of stages is not known in advance.
    assert all(item[1] is None for item in progress)

    response = json.loads(answered.content[0].text)
    assert response["result"]["rows"] == [{"total": 312765.9}]
    assert response["plan"][0]["query_language"] == "sql"

    # A client without progress support gets exactly the same product result.
    assert not quiet.isError
    assert json.loads(quiet.content[0].text) == response

    # The read-only guard is still enforced through MCP.
    assert unsafe.isError
    assert "unsafe_query" in unsafe.content[0].text


@pytest.mark.anyio
async def test_stdio_secure_datasource_tools(
    tmp_path: Path, server_parameters: StdioServerParameters
) -> None:
    """The secure datasource tools work over the real protocol, without a credential."""
    import sqlite3

    source = tmp_path / "sales.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, amount REAL)")
        connection.execute("INSERT INTO orders VALUES (1, 10.0)")

    profile = {"driver": "sqlite", "endpoint": {"path": str(source)}}

    async with (
        stdio_client(server_parameters) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        tested = await session.call_tool(
            "test_secure_datasource", {"kind": "relational", "connection_profile": profile}
        )
        created = await session.call_tool(
            "create_secure_datasource",
            {"name": "sales", "kind": "relational", "connection_profile": profile},
        )
        datasource = json.loads(created.content[0].text)
        scanned = await session.call_tool("scan_datasource", {"datasource_id": datasource["id"]})
        updated = await session.call_tool(
            "update_secure_datasource",
            {"datasource_id": datasource["id"], "connection_profile": profile},
        )
        deleted = await session.call_tool("delete_datasource", {"datasource_id": datasource["id"]})

    # A test saves nothing.
    assert json.loads(tested.content[0].text) == {
        "ok": True,
        "driver": "sqlite",
        "tls_enabled": False,
    }
    assert not created.isError
    assert datasource["status"] == "created"
    assert not scanned.isError
    # An update returns the datasource to ``created``; scan is the only way back to ready.
    assert json.loads(updated.content[0].text)["status"] == "created"
    assert not deleted.isError
    assert json.loads(deleted.content[0].text)["status"] == "deleted"


@pytest.mark.anyio
async def test_stdio_natural_language_question_contract(
    tmp_path: Path, server_parameters: StdioServerParameters
) -> None:
    """The fixed question contract, over MCP, against a real graph and model.

    Gated rather than weakened: this path cannot be exercised without a published graph and a model
    gateway, so without them it skips and says so.
    """
    if not model_gateway_ready():
        pytest.skip("model gateway not configured (QANERIS_MODEL_* unset)")
    if not graph_backend_ready():
        pytest.skip("published graph backend unreachable (QANERIS_NEO4J_URI)")

    demo_database = create_demo_database(tmp_path / "sales.db")
    cases = json.loads(
        (PROJECT_ROOT / "examples" / "questions_2026_07.json").read_text(encoding="utf-8")
    )["cases"]
    supported = [case for case in cases if case.get("supported_now", True)]

    async with (
        stdio_client(server_parameters) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        registration = await session.call_tool(
            "register_sqlite", {"name": "sales", "path": str(demo_database)}
        )
        datasource = json.loads(registration.content[0].text)
        await session.call_tool("scan_datasource", {"datasource_id": datasource["id"]})
        answers = [
            await session.call_tool(
                "ask_data", {"question": case["question"], "datasource_id": datasource["id"]}
            )
            for case in supported
        ]

    for answer, case in zip(answers, supported, strict=True):
        assert not answer.isError, case["id"]
        rows = json.loads(answer.content[0].text)["result"]["rows"]
        expected = case["expected_rows"]
        projected = [
            {key: row[key] for key in expected_row}
            for row, expected_row in zip(rows, expected, strict=True)
        ]
        assert projected == expected, case["id"]
