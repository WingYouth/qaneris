from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from qaneris.contracts import DatasetInfo, DatasetSample, NormalizedResult, RelationInfo


class DataSourceAdapter(ABC):
    def __init__(self, datasource_id: str, connection: dict[str, Any]):
        self.datasource_id = datasource_id
        self.connection = connection

    @abstractmethod
    def test_connection(self) -> None: ...

    @abstractmethod
    def scan_metadata(self) -> list[DatasetInfo]: ...

    def scan_relations(self) -> list[RelationInfo]:
        return []

    def scan_samples(self, datasets: list[DatasetInfo], limit: int = 3) -> list[DatasetSample]:
        """Return bounded samples; legacy third-party adapters may return none."""
        return []

    def scan_profile_records(
        self, datasets: list[DatasetInfo], limit: int = 20
    ) -> dict[str, list[dict[str, Any]]]:
        """Return bounded records for structural inference, never for persistence."""
        return {}

    @abstractmethod
    def execute(self, query: str, max_rows: int = 200) -> NormalizedResult: ...

    def execute_bound(
        self, query: str, parameters: tuple[Any, ...], max_rows: int = 200
    ) -> NormalizedResult:
        """Execute a parameterized command; formal query paths must use an implementing adapter."""
        raise NotImplementedError("This adapter does not support parameterized native execution")

    def execute_native(
        self, command: str | dict[str, Any], parameters: tuple[Any, ...] = (),
        max_rows: int = 200,
    ) -> NormalizedResult:
        """Formal execution port. Typed commands require an adapter implementation."""
        if isinstance(command, str):
            return self.execute_bound(command, parameters, max_rows)
        raise NotImplementedError("This adapter does not support typed native execution")
