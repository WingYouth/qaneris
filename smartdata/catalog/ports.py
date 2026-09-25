from __future__ import annotations

from typing import Protocol

from smartdata.contracts.datasource import DatasetInfo, DatasetSample, Datasource, RelationInfo
from smartdata.contracts.profile import CompanyDataProfile


class ProfileCatalogReader(Protocol):
    def get_company_data_profile(
        self, workspace_id: str = "default"
    ) -> CompanyDataProfile: ...


class SchemaCatalogReader(Protocol):
    def list_datasources(self, workspace_id: str = "default") -> list[Datasource]: ...

    def list_datasets(
        self, workspace_id: str = "default", datasource_id: str | None = None
    ) -> list[DatasetInfo]: ...

    def list_relations(
        self, workspace_id: str = "default", datasource_id: str | None = None
    ) -> list[RelationInfo]: ...

    def list_samples(
        self, workspace_id: str = "default", datasource_id: str | None = None
    ) -> list[DatasetSample]: ...
