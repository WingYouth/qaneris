"""The MCP product boundary: what the server may expose and what it may touch (RS-MCP-01).

Three separate guarantees, each checked against the real server rather than a description of it:

* **The tool surface.** The schemas a real MCP client sees are read back and inspected. No input may
  be able to carry a secret *value* - a password, token, key or certificate - and the legacy
  ``register_datasource`` bridge, which accepted an arbitrary connection JSON string, is gone.
* **The dependency boundary.** The server module's own source is read and scanned for the
  collaborators a product interface must not reach for. The Application Service uses catalogs,
  adapters and models; the MCP layer uses the Application Service.
* **The contract boundary.** The secure datasource tools are exercised through the real protocol,
  so the claim is about behavior, not about which names appear in the source.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session as connected_session

from smartdata.application.service import SmartDataService
from smartdata.catalog import Catalog
from smartdata.interfaces.mcp import server

SERVER_SOURCE = Path(server.__file__)

#: Field names that must never appear as a tool input path. Each one names a value a caller would
#: supply directly, and the whole point of the credential store is that MCP never sees one.
FORBIDDEN_INPUT_FIELDS = {
    "password",
    "token",
    "api_key",
    "secret",
    "private_key_password",
    "certificate_pem",
    "private_key_pem",
    "connection_json",
    "value",
}

#: Collaborators a product interface must reach only through the Application Service.
FORBIDDEN_COLLABORATORS = (
    "service.catalog",
    "service.model",
    "Catalog(",
    "build_schema_context(service.catalog",
    "ManagedCredentialStore",
    "SecretResolver",
    "TLSMaterializer",
    "GraphReader",
    "GraphStore",
    "create_adapter",
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def isolated_service(tmp_path, monkeypatch) -> SmartDataService:
    """A real service on a throwaway catalog, so protocol tests touch no shared state."""
    instance = SmartDataService(Catalog(tmp_path / "catalog.db"))
    monkeypatch.setattr(server, "service", instance)
    return instance


def schema_properties(schema: dict, prefix: str = "") -> list[tuple[str, dict]]:
    """Every ``(path, subschema)`` in a tool schema, including inside ``$defs``."""
    found: list[tuple[str, dict]] = []
    if isinstance(schema, dict):
        for name, child in schema.get("properties", {}).items():
            found.append((f"{prefix}/{name}", child))
            found.extend(schema_properties(child, f"{prefix}/{name}"))
        for name, child in schema.get("$defs", {}).items():
            found.extend(schema_properties(child, f"%defs:{name}"))
        for key in ("anyOf", "allOf", "oneOf", "items"):
            child = schema.get(key)
            if isinstance(child, list):
                for item in child:
                    found.extend(schema_properties(item, prefix))
            elif isinstance(child, dict):
                found.extend(schema_properties(child, prefix))
    return found


async def tool_schemas() -> dict[str, dict]:
    async with connected_session(server.mcp._mcp_server) as client:
        tools = (await client.list_tools()).tools
    return {tool.name: tool.inputSchema for tool in tools}


# -- tool surface ------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_no_tool_can_accept_a_raw_secret(isolated_service) -> None:
    for tool_name, schema in (await tool_schemas()).items():
        for path, subschema in schema_properties(schema):
            leaf = path.rsplit("/", 1)[-1].lower()
            if leaf not in FORBIDDEN_INPUT_FIELDS:
                continue
            # A field with a forbidden name is only acceptable when it is a SecretReference, i.e.
            # a {provider, identifier} pair that names a stored secret rather than carrying it.
            rendered = json.dumps(subschema)
            assert "SecretReference" in rendered, f"{tool_name}{path} accepts a raw value"


@pytest.mark.anyio
async def test_secret_reference_fields_expose_provider_and_identifier(isolated_service) -> None:
    """``password`` / ``client_private_key`` are allowed as *references*, never as strings."""
    schemas = await tool_schemas()
    schema = schemas["create_secure_datasource"]
    rendered = json.dumps(schema)
    assert "SecretProviderKind" in rendered
    definitions = schema.get("$defs", {})
    reference = definitions.get("SecretReference")
    assert reference is not None, "the reference type must be part of the published schema"
    assert set(reference["properties"]) == {"provider", "identifier"}
    for name, field in reference["properties"].items():
        # Neither half of a reference can carry a secret value.
        assert field.get("type") == "string" or "$ref" in field, name


@pytest.mark.anyio
async def test_no_field_is_an_untyped_json_string(isolated_service) -> None:
    """Connections are typed contracts, so an agent never parses a free-form JSON string."""
    for tool_name, schema in (await tool_schemas()).items():
        for path, subschema in schema_properties(schema):
            leaf = path.rsplit("/", 1)[-1].lower()
            if leaf == "connection_json":
                pytest.fail(f"{tool_name} still accepts connection_json")


@pytest.mark.anyio
async def test_legacy_register_datasource_is_gone(isolated_service) -> None:
    names = set(await tool_schemas())
    assert "register_datasource" not in names
    # And it is not merely unlisted - there is no second definition left in the module either.
    source = SERVER_SOURCE.read_text(encoding="utf-8")
    assert "def register_datasource" not in source


@pytest.mark.anyio
async def test_no_credential_creation_tool_exists(isolated_service) -> None:
    """Secrets are created through the CLI or the HTTP API, never through MCP."""
    names = set(await tool_schemas())
    forbidden = {
        "create_password",
        "upload_certificate",
        "upload_private_key",
        "credential_add",
        "create_managed_secret",
        "create_managed_client_identity",
    }
    assert names & forbidden == set()


@pytest.mark.anyio
async def test_roadshow_tool_set_is_present(isolated_service) -> None:
    names = set(await tool_schemas())
    assert {
        "list_adapters",
        "list_datasources",
        "inspect_datasource",
        "test_secure_datasource",
        "create_secure_datasource",
        "update_secure_datasource",
        "delete_datasource",
        "scan_datasource",
        "get_schema_context",
        "summarize_schema",
        "search_dataset",
        "list_relations",
        "list_mappings",
        "ask_data",
    }.issubset(names)


@pytest.mark.anyio
async def test_context_is_never_part_of_the_published_schema(isolated_service) -> None:
    """``Context`` is injected by the SDK and must stay invisible to a model."""
    schema = (await tool_schemas())["ask_data"]
    assert "ctx" not in schema.get("properties", {})
    assert "context" not in schema.get("properties", {})


# -- dependency boundary -----------------------------------------------------------------------


def test_server_source_reaches_no_collaborator_directly() -> None:
    """The MCP layer calls the Application Service and nothing beneath it."""
    source = SERVER_SOURCE.read_text(encoding="utf-8")
    for forbidden in FORBIDDEN_COLLABORATORS:
        assert forbidden not in source, f"MCP server reaches past the service: {forbidden}"


def test_server_imports_no_infrastructure_module() -> None:
    """A structural check: only the service and contracts may be imported as collaborators."""
    tree = ast.parse(SERVER_SOURCE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    allowed_prefixes = (
        "smartdata.application",
        "smartdata.contracts",
        "smartdata.common",
    )
    smartdata_imports = {name for name in imported if name.startswith("smartdata")}
    for name in smartdata_imports:
        assert name.startswith(allowed_prefixes), f"MCP imports a non-boundary module: {name}"


# -- contract boundary -------------------------------------------------------------------------


@pytest.mark.anyio
async def test_register_sqlite_goes_through_the_secure_contract(isolated_service, tmp_path) -> None:
    """The compatibility convenience produces a secure datasource that is not yet scanned."""
    import sqlite3

    source = tmp_path / "sales.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, amount REAL)")
        connection.execute("INSERT INTO orders VALUES (1, 10.0)")

    async with connected_session(server.mcp._mcp_server) as client:
        registered = await client.call_tool(
            "register_sqlite", {"name": "sales", "path": str(source)}
        )

    assert not registered.isError
    datasource = json.loads(registered.content[0].text)
    # Test → Save → Scan: registration provably does not scan.
    assert datasource["status"] == "created"
    detail = isolated_service.inspect_datasource(datasource["id"])
    assert detail.scan_version is None

    # And the stored connection really is a secure profile, not a legacy raw document.
    profile = isolated_service.catalog.get_connection_profile(datasource["id"])
    assert profile is not None
    assert profile.driver == "sqlite"


@pytest.mark.anyio
async def test_scan_stays_explicit_and_separate(isolated_service, tmp_path) -> None:
    import sqlite3

    source = tmp_path / "sales.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, amount REAL)")

    async with connected_session(server.mcp._mcp_server) as client:
        registered = await client.call_tool(
            "register_sqlite", {"name": "sales", "path": str(source)}
        )
        datasource = json.loads(registered.content[0].text)
        scanned = await client.call_tool("scan_datasource", {"datasource_id": datasource["id"]})

    assert not scanned.isError
    assert json.loads(scanned.content[0].text)
    assert isolated_service.inspect_datasource(datasource["id"]).scan_version is not None


@pytest.mark.anyio
async def test_test_secure_datasource_saves_nothing(isolated_service, tmp_path) -> None:
    """A test is read-only: it must not create a datasource as a side effect."""
    import sqlite3

    source = tmp_path / "sales.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY)")

    arguments = {
        "kind": "relational",
        "connection_profile": {"driver": "sqlite", "endpoint": {"path": str(source)}},
    }
    async with connected_session(server.mcp._mcp_server) as client:
        result = await client.call_tool("test_secure_datasource", arguments)

    assert not result.isError
    assert json.loads(result.content[0].text) == {
        "ok": True,
        "driver": "sqlite",
        "tls_enabled": False,
    }
    assert isolated_service.list_datasources() == []


@pytest.mark.anyio
async def test_create_secure_datasource_stops_at_created(isolated_service, tmp_path) -> None:
    import sqlite3

    source = tmp_path / "sales.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY)")

    async with connected_session(server.mcp._mcp_server) as client:
        created = await client.call_tool(
            "create_secure_datasource",
            {
                "name": "sales",
                "kind": "relational",
                "connection_profile": {"driver": "sqlite", "endpoint": {"path": str(source)}},
            },
        )

    assert not created.isError
    datasource = json.loads(created.content[0].text)
    assert datasource["status"] == "created"


@pytest.mark.anyio
async def test_delete_datasource_removes_it(isolated_service, tmp_path) -> None:
    import sqlite3

    source = tmp_path / "sales.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY)")

    async with connected_session(server.mcp._mcp_server) as client:
        registered = await client.call_tool(
            "register_sqlite", {"name": "sales", "path": str(source)}
        )
        datasource = json.loads(registered.content[0].text)
        deleted = await client.call_tool("delete_datasource", {"datasource_id": datasource["id"]})

    assert not deleted.isError
    assert isolated_service.list_datasources() == []


@pytest.mark.anyio
async def test_errors_keep_stable_codes_over_the_protocol(isolated_service) -> None:
    async with connected_session(server.mcp._mcp_server) as client:
        missing = await client.call_tool("inspect_datasource", {"datasource_id": "ds_missing"})

    assert missing.isError
    assert "datasource_not_found" in missing.content[0].text


@pytest.mark.anyio
async def test_unknown_failure_publishes_only_the_type(isolated_service, monkeypatch) -> None:
    def explode(*args, **kwargs):
        raise RuntimeError("driver said: password=hunter2 host=10.0.0.1")

    monkeypatch.setattr(isolated_service, "list_datasources", explode)

    async with connected_session(server.mcp._mcp_server) as client:
        result = await client.call_tool("list_datasources", {})

    assert result.isError
    text = result.content[0].text
    assert "RuntimeError" in text
    assert "hunter2" not in text
    assert "10.0.0.1" not in text
