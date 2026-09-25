from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class DatasourceKind(StrEnum):
    RELATIONAL = "relational"
    KEY_VALUE = "key_value"
    DOCUMENT = "document"
    WIDE_COLUMN = "wide_column"
    GRAPH = "graph"
    SEARCH = "search"
    TIME_SERIES = "time_series"
    OLAP = "olap"
    VECTOR = "vector"


class DatasourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: DatasourceKind
    connection: dict[str, Any]
    workspace_id: str = "default"


class Datasource(BaseModel):
    id: str
    name: str
    kind: DatasourceKind
    workspace_id: str
    driver: str | None = None
    status: str = "created"


class DatasourceDetail(BaseModel):
    """The public view of one datasource plus its current scan facts.

    Every field is either an existing ``Datasource`` field or a version/count read from the
    catalog's active ``ScanSnapshot``. The stored connection document, secret references and
    credentials are deliberately absent, so an interface layer may print this object as-is.
    """

    id: str
    name: str
    kind: DatasourceKind
    driver: str | None = None
    status: str = "created"
    workspace_id: str
    scan_version: int | None = None
    last_scan_status: str | None = None
    dataset_count: int | None = None


class FieldInfo(BaseModel):
    name: str
    data_type: str
    native_type: str | None = None
    nullable: bool = True
    primary_key: bool = False
    unique: bool = False
    indexed: bool = False
    default_value: Any | None = None
    comment: str | None = None


class IndexInfo(BaseModel):
    name: str
    fields: list[str] = Field(default_factory=list)
    unique: bool = False
    index_type: str | None = None
    native_options: dict[str, Any] = Field(default_factory=dict)


class ConstraintInfo(BaseModel):
    name: str
    constraint_type: str
    fields: list[str] = Field(default_factory=list)
    expression: str | None = None


class DatasetInfo(BaseModel):
    datasource_id: str
    name: str
    kind: str = "table"
    namespace: str | None = None
    comment: str | None = None
    estimated_record_count: int | None = Field(default=None, ge=0)
    fields: list[FieldInfo] = Field(default_factory=list)
    indexes: list[IndexInfo] = Field(default_factory=list)
    constraints: list[ConstraintInfo] = Field(default_factory=list)


class DatasetSample(BaseModel):
    datasource_id: str
    dataset: str
    rows: list[dict[str, Any]] = Field(default_factory=list, max_length=3)


class RelationInfo(BaseModel):
    datasource_id: str
    from_dataset: str
    from_field: str | None = None
    to_dataset: str
    to_field: str | None = None
    relation_type: str
    source: str = "scan"
