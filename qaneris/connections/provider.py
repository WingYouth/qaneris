"""Secure datasource connection provider."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from qaneris.common.errors import TLSFeatureUnsupportedError
from qaneris.connections.materializer import to_adapter_connection
from qaneris.connections.ports import ConnectionProfileReader
from qaneris.connections.secrets import SecretResolver
from qaneris.connections.tls_materializer import TLSMaterializer
from qaneris.contracts.connection import ConnectionProfile


class DatasourceConnectionProvider:
    """Opens datasource connections whose TLS material has a bounded lifetime.

    The formal runtime API is ``open()``. A TLS-enabled connection owns temporary certificate files
    that a driver reads when it opens a socket, so the connection dict is only valid inside the
    ``with`` block - that is where the materializer keeps the files alive and removes them after.
    """

    def __init__(
        self,
        catalog: ConnectionProfileReader | None,
        secret_resolver: SecretResolver | None = None,
        tls_materializer: TLSMaterializer | None = None,
    ):
        # The catalog is only needed for ``get()`` / ``open()``, which address a stored datasource.
        # ``open_profile()`` works on a candidate profile that is not stored yet, so a caller that
        # only tests candidates (the batch runner) can pass ``None``.
        self.catalog = catalog
        self.secret_resolver = secret_resolver or SecretResolver()
        self.tls_materializer = tls_materializer or TLSMaterializer()

    @contextmanager
    def open(self, datasource_id: str) -> Iterator[dict[str, Any]]:
        """Yield one runtime connection for a datasource, cleaning up on exit.

        A secure datasource resolves its profile, materializes its secrets and materializes its TLS
        parameters; a legacy datasource yields its stored raw connection and creates no TLS
        material at all.
        """
        _, legacy = self.catalog.get_datasource(datasource_id)
        profile = self.catalog.get_connection_profile(datasource_id)
        if profile is None:
            yield legacy
            return
        with self.open_profile(profile) as connection:
            yield connection

    @contextmanager
    def open_profile(self, profile: ConnectionProfile) -> Iterator[dict[str, Any]]:
        """Yield one runtime connection for a candidate profile that is not stored yet.

        This is what a connection test uses: the candidate is proven through exactly the same
        resolve → materialize path a stored datasource would take, so a test cannot pass on a
        connection shape the runtime would never build.
        """
        resolved = self.secret_resolver.materialize(profile)
        with self.tls_materializer.materialize(resolved) as tls_parameters:
            connection = to_adapter_connection(resolved)
            connection.update(tls_parameters)
            yield connection

    def get(self, datasource_id: str) -> dict[str, Any]:
        """Compatibility accessor for datasources whose connection has no bounded lifetime.

        Kept for legacy datasources and for TLS-disabled secure datasources, which need no
        temporary material and are safe to return as a plain dict. A TLS-enabled secure datasource
        is refused: returning it here would hand the caller a connection whose certificate files
        are already gone, so the caller is directed to the context-managed ``open()`` instead.
        """
        _, legacy = self.catalog.get_datasource(datasource_id)
        profile = self.catalog.get_connection_profile(datasource_id)
        if profile is None:
            return legacy
        if profile.tls.enabled:
            raise TLSFeatureUnsupportedError(
                f"datasource {datasource_id} has TLS enabled and must be opened with "
                f"ConnectionProvider.open() so its certificate material outlives the connection"
            )
        return to_adapter_connection(self.secret_resolver.materialize(profile))
