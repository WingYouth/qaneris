from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator

from smartdata.contracts.datasource import DatasourceKind


class DataObjectKind(StrEnum):
    TABLE = "table"
    VIEW = "view"
    COLLECTION = "collection"
    INDEX = "index"
    MEASUREMENT = "measurement"
    NODE_LABEL = "node_label"
    EDGE_TYPE = "edge_type"
    KEY_GROUP = "key_group"
    WIDE_COLUMN_TABLE = "wide_column_table"
    VECTOR_COLLECTION = "vector_collection"


class MetadataOrigin(StrEnum):
    DATABASE = "database"
    RULE = "rule"
    MODEL = "model"
    MANUAL = "manual"


class ScanStatus(StrEnum):
    CREATED = "created"
    VERIFYING = "verifying"
    CONNECTION_VERIFIED = "connection_verified"
    SCANNING = "scanning"
    PROFILING = "profiling"
    READY = "ready"
    CONNECTION_FAILED = "connection_failed"
    SCAN_FAILED = "scan_failed"
    PROFILE_FAILED = "profile_failed"
    STALE = "stale"


class DocumentStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


class ScanPolicy(BaseModel):
    profile_size: int = Field(default=20, ge=0, le=100)
    preview_size: int = Field(default=3, ge=0, le=3)
    field_sample_size: int = Field(default=10, ge=0, le=10)
    context_sample_size: int = Field(default=3, ge=0, le=3)
    max_data_objects: int = Field(default=1_000, ge=1, le=10_000)
    max_sample_bytes_per_object: int = Field(default=64_000, ge=1, le=1_000_000)
    timeout_seconds: int = Field(default=60, ge=1, le=3_600)


class SemanticMetadata(BaseModel):
    business_name: str | None = None
    description: str | None = None
    business_entity: str | None = None
    roles: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    origin: MetadataOrigin
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class FieldProfile(BaseModel):
    name: str
    path: str
    data_type: str
    native_type: str | None = None
    comment: str | None = None
    nullable: bool = True
    primary_key: bool = False
    unique: bool = False
    indexed: bool = False
    array: bool = False
    observed_count: int = Field(default=0, ge=0)
    sample_count: int = Field(default=0, ge=0)
    sample_values: list[Any] = Field(default_factory=list, max_length=10)
    semantic_metadata: list[SemanticMetadata] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_observation_counts(self) -> FieldProfile:
        if self.observed_count > self.sample_count:
            raise ValueError("observed_count cannot exceed sample_count")
        return self


class SamplePreview(BaseModel):
    values: dict[str, Any]
    redacted_fields: list[str] = Field(default_factory=list)


class DataObjectProfile(BaseModel):
    id: str
    datasource_id: str
    namespace: str | None = None
    name: str
    object_kind: DataObjectKind
    comment: str | None = None
    fields: list[FieldProfile] = Field(default_factory=list)
    estimated_record_count: int | None = Field(default=None, ge=0)
    profiled_record_count: int = Field(default=0, ge=0, le=100)
    previews: list[SamplePreview] = Field(default_factory=list, max_length=3)
    semantic_metadata: list[SemanticMetadata] = Field(default_factory=list)


class NamespaceProfile(BaseModel):
    name: str
    data_objects: list[DataObjectProfile] = Field(default_factory=list)


class RelationshipProfile(BaseModel):
    id: str
    from_datasource_id: str
    from_object_id: str
    from_field_path: str | None = None
    to_datasource_id: str
    to_object_id: str
    to_field_path: str | None = None
    relationship_type: str
    directed: bool = True
    origin: MetadataOrigin
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)
    confirmed: bool = False

    @property
    def cross_source(self) -> bool:
        return self.from_datasource_id != self.to_datasource_id


class DataSourceProfile(BaseModel):
    datasource_id: str
    name: str
    kind: DatasourceKind
    driver: str
    namespaces: list[NamespaceProfile] = Field(default_factory=list)
    semantic_metadata: list[SemanticMetadata] = Field(default_factory=list)


class ScanSnapshot(BaseModel):
    id: str
    datasource_id: str
    version: int = Field(ge=1)
    status: ScanStatus
    policy: ScanPolicy = Field(default_factory=ScanPolicy)
    profile: DataSourceProfile | None = None
    relationships: list[RelationshipProfile] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    document_status: DocumentStatus = DocumentStatus.PENDING
    document_path: str | None = None
    document_error: str | None = None
    started_at: datetime
    completed_at: datetime | None = None
    active: bool = False

    @model_validator(mode="after")
    def validate_ready_snapshot(self) -> ScanSnapshot:
        if self.status == ScanStatus.READY and (self.profile is None or self.completed_at is None):
            raise ValueError("ready snapshot requires profile and completed_at")
        if self.active and self.status != ScanStatus.READY:
            raise ValueError("only a ready snapshot can be active")
        return self


class CompanyDataProfile(BaseModel):
    workspace_id: str
    data_sources: list[DataSourceProfile] = Field(default_factory=list)
    relationships: list[RelationshipProfile] = Field(default_factory=list)
    generated_at: datetime
