from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from qaneris.contracts.connection import HostPort, ResolvedConnection

SQLSERVER_ODBC_DRIVER = "ODBC Driver 18 for SQL Server"
HOST_URL_DRIVERS: dict[str, tuple[str, str, int]] = {
    "couchdb": ("http", "https", 5984),
    "influxdb": ("http", "https", 8086),
    "milvus": ("http", "https", 19530),
    "neo4j": ("bolt", "bolt", 7687),
    "weaviate": ("http", "https", 8080),
}


def _sqlserver_query(query: str) -> str:
    parameters = parse_qsl(query, keep_blank_values=True)
    if not any(name == "driver" for name, _ in parameters):
        parameters.append(("driver", SQLSERVER_ODBC_DRIVER))
    return urlencode(parameters)


def to_adapter_connection(connection: ResolvedConnection) -> dict[str, Any]:
    """Translate the secure connection model at the adapter boundary only.

    This carries connection *addressing* and credentials, plus the non-secret TLS facts a driver
    needs to know about (whether TLS is on, whether to verify, a permitted server name, the minimum
    version). It deliberately does **not** carry TLS *material*: no CA PEM, client certificate or
    private key ever travels in this dict. Converting that material into driver parameters is
    ``TLSMaterializer``'s job alone, because only a materializer can own the lifetime of the
    temporary files some drivers require.
    """
    endpoint = connection.endpoint
    result = dict(connection.options)
    result["driver"] = connection.driver
    for name in ("path", "url", "database", "namespace", "project", "account"):
        value = getattr(endpoint, name)
        if value is not None:
            result[name] = value
    if endpoint.hosts:
        result["hosts"] = [item.model_dump(exclude_none=True) for item in endpoint.hosts]
        result["host"] = endpoint.hosts[0].host
        if endpoint.hosts[0].port is not None:
            result["port"] = endpoint.hosts[0].port
        if connection.driver in HOST_URL_DRIVERS:
            plain, secure, default_port = HOST_URL_DRIVERS[connection.driver]
            host = endpoint.hosts[0].host
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            port = endpoint.hosts[0].port or default_port
            scheme = secure if connection.tls_enabled else plain
            result["url"] = f"{scheme}://{host}:{port}"
    if connection.username is not None:
        result["username"] = connection.username
    result.update(connection.secrets)
    result.update(
        {
            "tls": connection.tls_enabled,
            "verify_server": connection.verify_server,
            "server_name": connection.server_name,
            "minimum_tls_version": connection.minimum_tls_version,
        }
    )
    # ``ssl`` / ``verify_certs`` are the legacy, non-material flags the Redis and search adapters
    # read for a TLS-disabled connection; a TLS-enabled connection gets its real parameters from
    # the materializer instead.
    result["ssl"] = connection.tls_enabled
    result["verify_certs"] = connection.verify_server
    if connection.driver == "cassandra" and endpoint.hosts:
        result["contact_points"] = [item.host for item in endpoint.hosts]
        if endpoint.namespace:
            result["keyspace"] = endpoint.namespace
    if connection.driver in {
        "postgresql",
        "mysql",
        "oracle",
        "sqlserver",
        "timescaledb",
        "clickhouse",
        "snowflake",
        "bigquery",
    }:
        result["url"] = sqlalchemy_url(connection)
    return result


def sqlalchemy_url(connection: ResolvedConnection) -> str:
    endpoint = connection.endpoint
    schemes = {
        "postgresql": "postgresql+psycopg",
        "timescaledb": "postgresql+psycopg",
        "mysql": "mysql+pymysql",
        "oracle": "oracle+oracledb",
        "sqlserver": "mssql+pyodbc",
        "clickhouse": "clickhousedb",
        "snowflake": "snowflake",
        "bigquery": "bigquery",
    }
    if endpoint.url:
        parsed = urlsplit(endpoint.url)
        scheme = schemes.get(connection.driver, parsed.scheme)
        host = parsed.hostname or ""
        netloc = _authenticated_netloc(connection, host, parsed.port)
        query = _sqlserver_query(parsed.query) if connection.driver == "sqlserver" else parsed.query
        return urlunsplit((scheme, netloc, parsed.path, query, parsed.fragment))
    if connection.driver == "bigquery":
        location = "/".join(filter(None, (endpoint.project, endpoint.namespace)))
        return f"bigquery://{location}"
    host = endpoint.hosts[0] if endpoint.hosts else HostPort(host=endpoint.account or "")
    netloc = _authenticated_netloc(connection, host.host, host.port)
    if connection.driver == "oracle":
        query = urlencode({"service_name": endpoint.database}) if endpoint.database else ""
        return urlunsplit((schemes[connection.driver], netloc, "", query, ""))
    path = "/" + "/".join(filter(None, (endpoint.database, endpoint.namespace)))
    query = _sqlserver_query("") if connection.driver == "sqlserver" else ""
    return urlunsplit((schemes[connection.driver], netloc, path, query, ""))


def _authenticated_netloc(connection: ResolvedConnection, host: str, port: int | None) -> str:
    authentication = ""
    if connection.username:
        authentication = quote(connection.username, safe="")
        if "password" in connection.secrets:
            authentication += ":" + quote(connection.secrets["password"], safe="")
        authentication += "@"
    return f"{authentication}{host}{f':{port}' if port is not None else ''}"
