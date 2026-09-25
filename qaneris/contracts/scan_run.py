from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ConnectionTestResult(BaseModel):
    name: str
    driver: str
    workspace_id: str
    status: str
    error: str | None = None


class ConnectionTestReport(BaseModel):
    status: str
    configured_datasources: int = Field(ge=0)
    connection_passed: int = Field(ge=0)
    connection_failed: int = Field(ge=0)
    results: list[ConnectionTestResult] = Field(default_factory=list)


class Neo4jValidation(BaseModel):
    status: str
    checks: dict[str, bool] = Field(default_factory=dict)
    expected: dict[str, int | str | None] = Field(default_factory=dict)
    actual: dict[str, int | str | None] = Field(default_factory=dict)
    error: str | None = None


class ScanRunMember(BaseModel):
    name: str
    driver: str
    kind: str
    workspace_id: str
    status: str
    connection_status: str
    scan_status: str
    datasource_id: str | None = None
    snapshot_id: str | None = None
    scan_version: int | None = None
    objects: int = Field(default=0, ge=0)
    fields: int = Field(default=0, ge=0)
    indexes: int = Field(default=0, ge=0)
    constraints: int = Field(default=0, ge=0)
    relationships: int = Field(default=0, ge=0)
    document_status: str | None = None
    document_path: str | None = None
    neo4j_status: str = "not_run"
    neo4j_validation: Neo4jValidation | None = None
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)


class ScanRunInfo(BaseModel):
    run_id: str
    workspace_id: str
    started_at: datetime
    completed_at: datetime | None = None
    status: str
    config_source: str


class ScanRunSummary(BaseModel):
    configured_datasources: int = Field(ge=0)
    connection_passed: int = Field(ge=0)
    scan_passed: int = Field(ge=0)
    scan_failed: int = Field(ge=0)
    total_data_objects: int = Field(ge=0)
    total_fields: int = Field(ge=0)
    total_indexes: int = Field(ge=0)
    total_constraints: int = Field(ge=0)
    total_relationships: int = Field(ge=0)


class ScanRunReport(BaseModel):
    run: ScanRunInfo
    summary: ScanRunSummary
    datasources: list[ScanRunMember] = Field(default_factory=list)
    neo4j_validation: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    failures: list[dict[str, str]] = Field(default_factory=list)
    artifact_paths: dict[str, str] = Field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.run.status == "passed"
