"""Runtime TLS materialization for driver connections (RS-CONN-01B).

The chain this module completes is::

    SecretReference → SecretResolver → ResolvedConnection → TLSMaterializer → driver kwargs

Its whole reason to be a *context manager* is file lifetime. A driver that takes certificate paths
reads them when it opens a socket - later than this function returns. Creating a temp file and
returning its path would leave the adapter holding a path to a file that may already be gone. So
the runtime connection is only valid inside ``with materialize(...)``, and the temporary directory
is removed on every exit path: success, connection failure, scan failure, adapter exception and
``KeyboardInterrupt`` alike.

Three invariants hold for everything in here:

* **Validate before the driver runs.** Certificate material is re-checked against
  ``CertificateValidator`` on every connection, even when it came from the managed store and was
  validated at upload: a certificate can expire and an environment/file reference can be repointed
  between scans. A mismatched client pair is found here, not by a production database handshake.
* **Fail closed on unsupported capability.** Requests are checked against ``TLS_DRIVER_MATRIX``.
  An unsupported feature raises ``TLSFeatureUnsupportedError`` - verification is never quietly
  disabled and trust material is never silently dropped to make a connection succeed.
* **Never leak material.** No certificate body, private key, key password or CA content is placed
  in the adapter dict, a URL, an exception message or a log line. What travels onward is a file
  path inside a private directory or an ``SSLContext`` object.
"""

from __future__ import annotations

import os
import shutil
import ssl
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from smartdata.common.errors import (
    TLSFeatureUnsupportedError,
    TLSMaterializationError,
)
from smartdata.connections.certificate_validator import CertificateValidator
from smartdata.connections.materializer import sqlalchemy_url
from smartdata.connections.tls_matrix import TLS_DRIVER_MATRIX, TLSDriverSpec, TLSStrategy
from smartdata.contracts.connection import ResolvedConnection

#: Fixed names inside a random, private directory - the directory is what makes them unguessable.
CA_FILE = "ca.pem"
CLIENT_CERT_FILE = "client-cert.pem"
CLIENT_KEY_FILE = "client-key.pem"
CLIENT_COMBINED_FILE = "client-combined.pem"

_MINIMUM_VERSIONS = {
    "1.2": ssl.TLSVersion.TLSv1_2,
    "1.3": ssl.TLSVersion.TLSv1_3,
}


class TLSMaterializer:
    """Turns resolved TLS secrets into driver parameters with a bounded lifetime."""

    def __init__(self, validator: CertificateValidator | None = None):
        self.validator = validator or CertificateValidator()

    @contextmanager
    def materialize(self, connection: ResolvedConnection) -> Iterator[dict[str, Any]]:
        """Yield the TLS parameters for one connection, cleaning up before returning.

        With TLS disabled this yields nothing and creates no directory at all - a non-TLS datasource
        must not gain a temp directory, a file or an ``SSLContext`` because this module exists.
        """
        if not connection.tls_enabled:
            yield {}
            return

        spec = TLS_DRIVER_MATRIX.get(connection.driver)
        if spec is None:
            raise TLSFeatureUnsupportedError(
                f"driver {connection.driver} is not in the TLS driver matrix"
            )
        self._validate_capabilities(spec, connection)

        material = self._normalize_material(connection)
        temp_directory: Path | None = None
        try:
            if self._needs_files(spec, material):
                temp_directory = self._create_temp_directory()
                self._write_material_files(temp_directory, material)
            yield self._adapter_parameters(spec, connection, material, temp_directory)
        finally:
            self._cleanup(temp_directory)

    # ------------------------------------------------------------------------------------
    # capability validation
    # ------------------------------------------------------------------------------------

    def _validate_capabilities(
        self, spec: TLSDriverSpec, connection: ResolvedConnection
    ) -> None:
        """Refuse anything the matrix does not grant, before a driver is touched."""
        material = connection.tls_material
        if "ca_certificate" in material and not spec.custom_ca:
            raise TLSFeatureUnsupportedError(
                f"driver {spec.driver} does not support a custom CA certificate"
            )
        if (
            "client_certificate" in material or "client_private_key" in material
        ) and not spec.mtls:
            raise TLSFeatureUnsupportedError(
                f"driver {spec.driver} does not support client certificate authentication"
            )
        if connection.minimum_tls_version == "1.3" and not spec.tls13_control:
            raise TLSFeatureUnsupportedError(
                f"driver {spec.driver} cannot enforce a TLS 1.3 minimum version"
            )
        # A driver without server-name support can only accept the endpoint's own hostname: any
        # other name would be a silent no-op the caller did not ask for.
        if (
            connection.server_name is not None
            and not spec.server_name_override
            and connection.server_name != self._endpoint_host(connection)
        ):
            raise TLSFeatureUnsupportedError(
                f"driver {spec.driver} cannot override the TLS server name"
            )

    @staticmethod
    def _endpoint_host(connection: ResolvedConnection) -> str | None:
        if connection.endpoint.hosts:
            return connection.endpoint.hosts[0].host
        if connection.endpoint.url:
            return urlsplit(connection.endpoint.url).hostname
        return None

    # ------------------------------------------------------------------------------------
    # runtime material validation
    # ------------------------------------------------------------------------------------

    def _normalize_material(self, connection: ResolvedConnection) -> dict[str, str]:
        """Re-validate and canonicalize the material that is about to be handed to a driver.

        Every rejection here is a ``TLSMaterializationError``: the material resolved, but by the
        time the connection is opening it is no longer usable.
        """
        material = connection.tls_material
        normalized: dict[str, str] = {}
        try:
            if "ca_certificate" in material:
                normalized["ca_certificate"], _ = self.validator.normalize_ca_certificate(
                    material["ca_certificate"]
                )
            if "client_certificate" in material:
                normalized["client_certificate"], _ = (
                    self.validator.normalize_client_certificate(material["client_certificate"])
                )
            if "client_private_key" in material:
                normalized["client_private_key"], _ = self.validator.normalize_private_key(
                    material["client_private_key"],
                    password=material.get("client_private_key_password"),
                )
        except TLSMaterializationError:
            raise
        except Exception as error:
            # A certificate problem is a materialization problem at this boundary; the original
            # message already names the problem without carrying the material.
            raise TLSMaterializationError(str(error)) from error

        if "client_certificate" in normalized and "client_private_key" in normalized:
            try:
                self.validator.validate_client_pair(
                    normalized["client_certificate"], normalized["client_private_key"]
                )
            except TLSMaterializationError:
                raise
            except Exception as error:
                raise TLSMaterializationError(str(error)) from error
        return normalized

    # ------------------------------------------------------------------------------------
    # temporary directory lifecycle
    # ------------------------------------------------------------------------------------

    @staticmethod
    def _needs_files(spec: TLSDriverSpec, material: dict[str, str]) -> bool:
        """A directory is created only when a driver actually needs a path.

        An ``SSL_CONTEXT`` driver with no client pair needs none: a custom CA is loaded straight
        from memory via ``load_verify_locations(cadata=...)``.
        """
        if spec.strategy is TLSStrategy.SSL_CONTEXT:
            return "client_certificate" in material
        return bool(material)

    @staticmethod
    def _create_temp_directory() -> Path:
        directory = Path(tempfile.mkdtemp(prefix="smartdata-tls-"))
        os.chmod(directory, 0o700)
        return directory

    def _write_material_files(self, directory: Path, material: dict[str, str]) -> None:
        """Write material with explicit permissions, never through a default-mode write helper."""
        if "ca_certificate" in material:
            self._write_private_file(directory / CA_FILE, material["ca_certificate"])
        if "client_certificate" in material:
            self._write_private_file(
                directory / CLIENT_CERT_FILE, material["client_certificate"]
            )
        if "client_private_key" in material:
            self._write_private_file(directory / CLIENT_KEY_FILE, material["client_private_key"])
        if "client_certificate" in material and "client_private_key" in material:
            # pymongo wants one file holding the key followed by the certificate. The key was
            # already unlocked and normalized to unencrypted PKCS#8, so the combined file carries
            # no password.
            combined = material["client_private_key"] + material["client_certificate"]
            self._write_private_file(directory / CLIENT_COMBINED_FILE, combined)

    @staticmethod
    def _write_private_file(path: Path, content: str) -> None:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _cleanup(directory: Path | None) -> None:
        """Remove the temporary directory on every path, and never log what was in it."""
        if directory is None:
            return
        shutil.rmtree(directory, ignore_errors=True)

    # ------------------------------------------------------------------------------------
    # ssl context
    # ------------------------------------------------------------------------------------

    def _build_ssl_context(
        self,
        connection: ResolvedConnection,
        material: dict[str, str],
        directory: Path | None,
    ) -> ssl.SSLContext:
        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        context.minimum_version = _MINIMUM_VERSIONS[connection.minimum_tls_version]
        if connection.verify_server:
            context.verify_mode = ssl.CERT_REQUIRED
            context.check_hostname = True
        else:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        if "ca_certificate" in material:
            try:
                context.load_verify_locations(cadata=material["ca_certificate"])
            except ssl.SSLError as error:
                raise TLSMaterializationError(
                    f"CA certificate could not be loaded: {type(error).__name__}"
                ) from error
        if "client_certificate" in material:
            if directory is None:  # pragma: no cover - guarded by _needs_files
                raise TLSMaterializationError("client certificate requires a material directory")
            try:
                context.load_cert_chain(
                    certfile=str(directory / CLIENT_CERT_FILE),
                    keyfile=str(directory / CLIENT_KEY_FILE),
                )
            except ssl.SSLError as error:
                raise TLSMaterializationError(
                    f"client certificate and key could not be loaded: {type(error).__name__}"
                ) from error
        return context

    # ------------------------------------------------------------------------------------
    # strategy → driver parameters
    # ------------------------------------------------------------------------------------

    def _adapter_parameters(
        self,
        spec: TLSDriverSpec,
        connection: ResolvedConnection,
        material: dict[str, str],
        directory: Path | None,
    ) -> dict[str, Any]:
        builders = {
            TLSStrategy.SSL_CONTEXT: self._ssl_context_parameters,
            TLSStrategy.POSTGRES_FILES: self._postgres_parameters,
            TLSStrategy.SQLSERVER_ODBC: self._sqlserver_parameters,
            TLSStrategy.MONGODB_FILES: self._mongodb_parameters,
            TLSStrategy.REDIS_FILES: self._redis_parameters,
            TLSStrategy.CLICKHOUSE_FILES: self._clickhouse_parameters,
            TLSStrategy.INFLUXDB_FILES: self._influxdb_parameters,
            TLSStrategy.MILVUS_FILES: self._milvus_parameters,
            TLSStrategy.SYSTEM_TRUST_ONLY: self._system_trust_parameters,
        }
        return builders[spec.strategy](connection, material, directory)

    def _ssl_context_parameters(
        self,
        connection: ResolvedConnection,
        material: dict[str, str],
        directory: Path | None,
    ) -> dict[str, Any]:
        """Build one ``SSLContext`` and place it where this driver's client expects it.

        The context is always the same object; only its destination differs. MySQL's SQLAlchemy
        dialect takes it as a pymysql connect argument, Neo4j takes it as a driver argument (with
        its URI normalized off the ``+s`` schemes so the two are never combined), and the HTTP-based
        adapters take it as their ``verify`` argument.
        """
        context = self._build_ssl_context(connection, material, directory)
        parameters: dict[str, Any] = {"ssl": True}

        if connection.driver == "mysql":
            parameters["connect_args"] = {"ssl": context}
        elif connection.driver == "neo4j":
            parameters["ssl_context"] = context
            if connection.endpoint.url:
                # A ``+s``/``+ssc`` scheme already means "drivers own the TLS config", which cannot
                # be combined with an SSLContext. Normalize to the plain scheme exactly once.
                parameters["url"] = _downgrade_neo4j_scheme(connection.endpoint.url)
        elif connection.driver == "elasticsearch":
            # An SSLContext already carries the CA, the client pair and the verification policy;
            # passing ca_certs/client_cert alongside it would be a second, competing TLS config.
            parameters["ssl_context"] = context
        elif connection.driver == "opensearch":
            parameters["ssl_context"] = context
            parameters["use_ssl"] = True
            parameters["verify_certs"] = connection.verify_server
        elif connection.driver == "cassandra":
            parameters["ssl_context"] = context
            if connection.server_name is not None:
                # Cassandra carries SNI in ssl_options rather than in the context.
                parameters["ssl_options"] = {"server_hostname": connection.server_name}
        else:
            # CouchDB and Weaviate reach TLS through httpx, whose ``verify`` accepts a context.
            parameters["ssl_context"] = context
            if connection.endpoint.url:
                parameters["url"] = _upgrade_scheme(connection.endpoint.url, "http", "https")
        return parameters

    def _postgres_parameters(
        self,
        connection: ResolvedConnection,
        material: dict[str, str],
        directory: Path | None,
    ) -> dict[str, Any]:
        connect_args: dict[str, Any] = {
            "sslmode": "verify-full" if connection.verify_server else "require",
            "ssl_min_protocol_version": f"TLSv{connection.minimum_tls_version}",
        }
        if "ca_certificate" in material:
            connect_args["sslrootcert"] = str(Path(directory) / CA_FILE)  # type: ignore[arg-type]
        if "client_certificate" in material:
            connect_args["sslcert"] = str(Path(directory) / CLIENT_CERT_FILE)  # type: ignore[arg-type]
            connect_args["sslkey"] = str(Path(directory) / CLIENT_KEY_FILE)  # type: ignore[arg-type]
        return {"connect_args": connect_args}

    def _sqlserver_parameters(
        self,
        connection: ResolvedConnection,
        material: dict[str, str],
        directory: Path | None,
    ) -> dict[str, Any]:
        """Server TLS only, expressed in the ODBC URL query.

        Custom CA, client material and a TLS 1.3 floor were already refused by capability
        validation, so nothing here can silently map a CA onto ``ServerCertificate`` pinning.
        """
        query: dict[str, str] = {
            "Encrypt": "yes",
            "TrustServerCertificate": "yes" if not connection.verify_server else "no",
        }
        if connection.server_name is not None:
            query["HostNameInCertificate"] = connection.server_name
        return {"url": _with_query(sqlalchemy_url(connection), query)}

    def _mongodb_parameters(
        self,
        connection: ResolvedConnection,
        material: dict[str, str],
        directory: Path | None,
    ) -> dict[str, Any]:
        parameters: dict[str, Any] = {
            "tls": True,
            "tlsAllowInvalidCertificates": not connection.verify_server,
            "tlsAllowInvalidHostnames": not connection.verify_server,
        }
        if "ca_certificate" in material:
            parameters["tlsCAFile"] = str(Path(directory) / CA_FILE)  # type: ignore[arg-type]
        if "client_certificate" in material:
            parameters["tlsCertificateKeyFile"] = str(  # type: ignore[arg-type]
                Path(directory) / CLIENT_COMBINED_FILE
            )
        return parameters

    def _redis_parameters(
        self,
        connection: ResolvedConnection,
        material: dict[str, str],
        directory: Path | None,
    ) -> dict[str, Any]:
        parameters: dict[str, Any] = {
            "ssl": True,
            "ssl_cert_reqs": ssl.CERT_REQUIRED if connection.verify_server else ssl.CERT_NONE,
            "ssl_check_hostname": connection.verify_server,
            "ssl_min_version": _MINIMUM_VERSIONS[connection.minimum_tls_version],
        }
        if "ca_certificate" in material:
            parameters["ssl_ca_certs"] = str(Path(directory) / CA_FILE)  # type: ignore[arg-type]
        if "client_certificate" in material:
            parameters["ssl_certfile"] = str(Path(directory) / CLIENT_CERT_FILE)  # type: ignore[arg-type]
            parameters["ssl_keyfile"] = str(Path(directory) / CLIENT_KEY_FILE)  # type: ignore[arg-type]
        if connection.endpoint.url:
            # A single deterministic normalization: the profile asked for TLS, so the URL must not
            # keep speaking plaintext. Nothing but the scheme changes.
            parameters["url"] = _upgrade_scheme(connection.endpoint.url, "redis", "rediss")
        return parameters

    def _clickhouse_parameters(
        self,
        connection: ResolvedConnection,
        material: dict[str, str],
        directory: Path | None,
    ) -> dict[str, Any]:
        connect_args: dict[str, Any] = {
            "secure": True,
            "verify": connection.verify_server,
        }
        if "ca_certificate" in material:
            connect_args["ca_cert"] = str(Path(directory) / CA_FILE)  # type: ignore[arg-type]
        if "client_certificate" in material:
            connect_args["client_cert"] = str(Path(directory) / CLIENT_CERT_FILE)  # type: ignore[arg-type]
            connect_args["client_cert_key"] = str(Path(directory) / CLIENT_KEY_FILE)  # type: ignore[arg-type]
            connect_args["tls_mode"] = "mutual"
        if connection.server_name is not None:
            connect_args["server_host_name"] = connection.server_name
        return {"connect_args": connect_args}

    def _influxdb_parameters(
        self,
        connection: ResolvedConnection,
        material: dict[str, str],
        directory: Path | None,
    ) -> dict[str, Any]:
        parameters: dict[str, Any] = {
            "verify_ssl": connection.verify_server,
            # The key was unlocked and normalized to unencrypted PKCS#8, so the client needs none.
            "cert_key_password": None,
        }
        if "ca_certificate" in material:
            parameters["ssl_ca_cert"] = str(Path(directory) / CA_FILE)  # type: ignore[arg-type]
        if "client_certificate" in material:
            parameters["cert_file"] = str(Path(directory) / CLIENT_CERT_FILE)  # type: ignore[arg-type]
            parameters["cert_key_file"] = str(Path(directory) / CLIENT_KEY_FILE)  # type: ignore[arg-type]
        if connection.endpoint.url:
            parameters["url"] = _upgrade_scheme(connection.endpoint.url, "http", "https")
        return parameters

    def _milvus_parameters(
        self,
        connection: ResolvedConnection,
        material: dict[str, str],
        directory: Path | None,
    ) -> dict[str, Any]:
        parameters: dict[str, Any] = {"secure": True}
        if "ca_certificate" in material:
            parameters["ca_pem_path"] = str(Path(directory) / CA_FILE)  # type: ignore[arg-type]
        if "client_certificate" in material:
            parameters["client_pem_path"] = str(Path(directory) / CLIENT_CERT_FILE)  # type: ignore[arg-type]
            parameters["client_key_path"] = str(Path(directory) / CLIENT_KEY_FILE)  # type: ignore[arg-type]
        if connection.server_name is not None:
            parameters["server_name"] = connection.server_name
        return parameters

    def _system_trust_parameters(
        self,
        connection: ResolvedConnection,
        material: dict[str, str],
        directory: Path | None,
    ) -> dict[str, Any]:
        """HTTPS against the driver's own trust store - no custom material of any kind."""
        parameters: dict[str, Any] = {
            "https": True,
            "verify": connection.verify_server,
        }
        if connection.endpoint.url:
            parameters["url"] = _upgrade_scheme(connection.endpoint.url, "http", "https")
        return parameters


def _with_query(url: str, query: dict[str, str]) -> str:
    """Add query parameters to a URL without touching its host, port, path or credentials."""
    parts = urlsplit(url)
    merged = dict(parse_qsl(parts.query, keep_blank_values=True))
    merged.update(query)
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(merged), parts.fragment)
    )


def _upgrade_scheme(url: str, plain: str, secure: str) -> str:
    """Rewrite only the scheme, and only from the plaintext form to its TLS form."""
    parts = urlsplit(url)
    if parts.scheme != plain:
        return url
    return urlunsplit((secure, parts.netloc, parts.path, parts.query, parts.fragment))


def _downgrade_neo4j_scheme(url: str) -> str:
    """Drop Neo4j's ``+s`` / ``+ssc`` TLS marker, keeping the plain scheme it decorates.

    ``bolt+s`` / ``bolt+ssc`` tell the driver to build its own TLS config; when an ``SSLContext`` is
    supplied instead the plain ``bolt`` scheme is required, and using both at once is an error.
    """
    parts = urlsplit(url)
    base = parts.scheme.split("+", 1)[0]
    if base not in {"bolt", "neo4j"}:
        return url
    return urlunsplit((base, parts.netloc, parts.path, parts.query, parts.fragment))


__all__ = ["TLSMaterializer"]
