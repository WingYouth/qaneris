from __future__ import annotations

from typing import Any

from smartdata.adapters.base import DataSourceAdapter
from smartdata.adapters.registry import create_adapter as create_registered_adapter
from smartdata.contracts import DatasourceKind


def create_adapter(
    datasource_id: str, kind: DatasourceKind, connection: dict[str, Any]
) -> DataSourceAdapter:
    return create_registered_adapter(datasource_id, kind, connection)
