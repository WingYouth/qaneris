"""Driver TLS capability matrix (RS-CONN-01B).

This module is the single source of truth for *what each driver can be asked to do over TLS*. It
records capability, not preference: a driver is marked as supporting custom CA, mTLS, a hostname
override or TLS 1.3 control only when its client library actually carries that capability.

Two rules make this file load-bearing:

* **Adapters never re-declare capability.** ``TLSMaterializer`` validates every request against this
  matrix before touching a driver, so a profile asking for something the driver cannot do fails
  closed with ``TLSFeatureUnsupportedError`` instead of being quietly ignored.
* **Capability here is not deployment acceptance.** A ``yes`` means the code can express the
  feature; it does not mean a particular server has been tested with it. The Roadshow TLS
  exemption for the two non-TLS targets is decided later by real handshakes (RS-TLS-01), never by
  editing this table.

The 16 rows below are the Roadshow target set and are a fixed implementation specification for this
card. If reality disagrees after real-server acceptance, the next stage changes the specification
deliberately - the implementation never guesses.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class TLSStrategy(StrEnum):
    """How one driver receives its TLS material at runtime.

    The strategy decides which shape of adapter parameter the materializer produces: a live
    ``SSLContext`` object, or a set of files it must own for the duration of the connection.
    """

    #: A Python ``SSLContext`` handed to a library that accepts one (httpx, cassandra, neo4j, ...).
    SSL_CONTEXT = "ssl_context"
    #: ``connect_args`` for psycopg: ``sslmode`` plus ``sslrootcert``/``sslcert``/``sslkey`` paths.
    POSTGRES_FILES = "postgres_files"
    #: ODBC URL query parameters; server TLS only, no client material.
    SQLSERVER_ODBC = "sqlserver_odbc"
    #: pymongo ``tls*`` options, with CA and a combined cert+key PEM as file paths.
    MONGODB_FILES = "mongodb_files"
    #: redis-py ``ssl_*`` options, with CA/cert/key as file paths.
    REDIS_FILES = "redis_files"
    #: clickhouse-connect path options for CA, client cert and client key.
    CLICKHOUSE_FILES = "clickhouse_files"
    #: influxdb-client ``ssl_ca_cert``/``cert_file``/``cert_key_file`` paths.
    INFLUXDB_FILES = "influxdb_files"
    #: pymilvus ``ca_pem_path``/``client_pem_path``/``client_key_path`` options.
    MILVUS_FILES = "milvus_files"
    #: The driver's own trust store only: HTTPS with verification, no custom CA and no mTLS.
    SYSTEM_TRUST_ONLY = "system_trust_only"


@dataclass(frozen=True)
class TLSDriverSpec:
    """The TLS capabilities of one driver.

    ``custom_ca``, ``mtls``, ``server_name_override`` and ``tls13_control`` are exactly the four
    questions the materializer asks before accepting a profile. ``strategy`` is how a supported
    request is then delivered to the driver.
    """

    driver: str
    strategy: TLSStrategy
    custom_ca: bool
    mtls: bool
    server_name_override: bool
    tls13_control: bool


def _spec(
    driver: str,
    strategy: TLSStrategy,
    *,
    custom_ca: bool,
    mtls: bool,
    server_name_override: bool,
    tls13_control: bool,
) -> TLSDriverSpec:
    return TLSDriverSpec(
        driver=driver,
        strategy=strategy,
        custom_ca=custom_ca,
        mtls=mtls,
        server_name_override=server_name_override,
        tls13_control=tls13_control,
    )


#: The Roadshow driver set. Sixteen rows, one per target; a driver missing here cannot be connected
#: with TLS at all, and the matrix test asserts this set is complete.
TLS_DRIVER_MATRIX: dict[str, TLSDriverSpec] = {
    spec.driver: spec
    for spec in (
        _spec(
            "postgresql",
            TLSStrategy.POSTGRES_FILES,
            custom_ca=True,
            mtls=True,
            server_name_override=False,
            tls13_control=True,
        ),
        _spec(
            "timescaledb",
            TLSStrategy.POSTGRES_FILES,
            custom_ca=True,
            mtls=True,
            server_name_override=False,
            tls13_control=True,
        ),
        _spec(
            "mysql",
            TLSStrategy.SSL_CONTEXT,
            custom_ca=True,
            mtls=True,
            server_name_override=False,
            tls13_control=True,
        ),
        _spec(
            "sqlserver",
            TLSStrategy.SQLSERVER_ODBC,
            custom_ca=False,
            mtls=False,
            server_name_override=True,
            tls13_control=False,
        ),
        _spec(
            "mongodb",
            TLSStrategy.MONGODB_FILES,
            custom_ca=True,
            mtls=True,
            server_name_override=False,
            tls13_control=False,
        ),
        _spec(
            "redis",
            TLSStrategy.REDIS_FILES,
            custom_ca=True,
            mtls=True,
            server_name_override=False,
            tls13_control=True,
        ),
        _spec(
            "couchdb",
            TLSStrategy.SSL_CONTEXT,
            custom_ca=True,
            mtls=True,
            server_name_override=False,
            tls13_control=True,
        ),
        _spec(
            "cassandra",
            TLSStrategy.SSL_CONTEXT,
            custom_ca=True,
            mtls=True,
            server_name_override=True,
            tls13_control=True,
        ),
        _spec(
            "clickhouse",
            TLSStrategy.CLICKHOUSE_FILES,
            custom_ca=True,
            mtls=True,
            server_name_override=True,
            tls13_control=False,
        ),
        _spec(
            "elasticsearch",
            TLSStrategy.SSL_CONTEXT,
            custom_ca=True,
            mtls=True,
            server_name_override=False,
            tls13_control=True,
        ),
        _spec(
            "opensearch",
            TLSStrategy.SSL_CONTEXT,
            custom_ca=True,
            mtls=True,
            server_name_override=True,
            tls13_control=True,
        ),
        _spec(
            "influxdb",
            TLSStrategy.INFLUXDB_FILES,
            custom_ca=True,
            mtls=True,
            server_name_override=False,
            tls13_control=False,
        ),
        _spec(
            "neo4j",
            TLSStrategy.SSL_CONTEXT,
            custom_ca=True,
            mtls=True,
            server_name_override=False,
            tls13_control=True,
        ),
        _spec(
            "milvus",
            TLSStrategy.MILVUS_FILES,
            custom_ca=True,
            mtls=True,
            server_name_override=True,
            tls13_control=False,
        ),
        # Qdrant is HTTPS with the system trust store and nothing else. Its REST transport reaches
        # TLS through httpx kwargs, where SSLContext lifetime and copying are version-sensitive, so
        # this card refuses custom CA and mTLS for it rather than mutating process-global trust via
        # SSL_CERT_FILE / REQUESTS_CA_BUNDLE - one datasource must never change another's trust.
        _spec(
            "qdrant",
            TLSStrategy.SYSTEM_TRUST_ONLY,
            custom_ca=False,
            mtls=False,
            server_name_override=False,
            tls13_control=False,
        ),
        _spec(
            "weaviate",
            TLSStrategy.SSL_CONTEXT,
            custom_ca=True,
            mtls=True,
            server_name_override=False,
            tls13_control=True,
        ),
    )
}


def tls_spec(driver: str) -> TLSDriverSpec | None:
    """Return the TLS specification for a driver, or ``None`` when it has no TLS support."""
    return TLS_DRIVER_MATRIX.get(driver)
