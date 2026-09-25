from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Protocol

from qaneris.contracts.connection import ConnectionProfile
from qaneris.contracts.datasource import Datasource


class ConnectionProfileReader(Protocol):
    def get_datasource(self, datasource_id: str) -> tuple[Datasource, dict[str, Any]]: ...

    def get_connection_profile(self, datasource_id: str) -> ConnectionProfile | None: ...


class ConnectionProvider(Protocol):
    """The formal runtime API for obtaining a datasource connection.

    ``open()`` is context-managed because a TLS-enabled connection owns temporary certificate
    material that must outlive the driver call and be removed afterwards. ``get()`` remains for
    legacy and TLS-disabled datasources only.
    """

    def open(self, datasource_id: str) -> AbstractContextManager[dict[str, Any]]: ...

    def open_profile(
        self, profile: ConnectionProfile
    ) -> AbstractContextManager[dict[str, Any]]: ...

    def get(self, datasource_id: str) -> dict[str, Any]: ...
