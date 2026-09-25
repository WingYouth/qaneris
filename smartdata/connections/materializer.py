from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from smartdata.contracts.connection import HostPort, ResolvedConnection


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
        return urlunsplit((scheme, netloc, parsed.path, parsed.query, parsed.fragment))
    if connection.driver == "bigquery":
        location = "/".join(filter(None, (endpoint.project, endpoint.namespace)))
        return f"bigquery://{location}"
    host = endpoint.hosts[0] if endpoint.hosts else HostPort(host=endpoint.account or "")
    netloc = _authenticated_netloc(connection, host.host, host.port)
    path = "/" + "/".join(filter(None, (endpoint.database, endpoint.namespace)))
    return urlunsplit((schemes[connection.driver], netloc, path, "", ""))


def _authenticated_netloc(connection: ResolvedConnection, host: str, port: int | None) -> str:
    authentication = ""
    if connection.username:
        authentication = quote(connection.username, safe="")
        if "password" in connection.secrets:
            authentication += ":" + quote(connection.secrets["password"], safe="")
        authentication += "@"
    return f"{authentication}{host}{f':{port}' if port is not None else ''}"
