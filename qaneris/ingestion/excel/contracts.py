"""Public contracts and the stable error model of the Excel ingestion slice.

These are the only types an interface layer (CLI, API, MCP, Skill) is allowed to touch: validation,
materialization and idempotency stay behind :class:`ExcelIngestionService`, and the datasource /
scan / Neo4j steps reuse the existing secure datasource chain.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from qaneris.common.errors import QanerisError

MANIFEST_FORMAT_VERSION = "excel-manifest-v1"


class ExcelErrorCode(StrEnum):
    """Stable machine codes. Codes never carry a business cell value."""

    INVALID_EXTENSION = "EXCEL_INVALID_EXTENSION"
    INVALID_CONTAINER = "EXCEL_INVALID_CONTAINER"
    FILE_TOO_LARGE = "EXCEL_FILE_TOO_LARGE"
    ZIP_LIMIT_EXCEEDED = "EXCEL_ZIP_LIMIT_EXCEEDED"
    NO_IMPORTABLE_SHEET = "EXCEL_NO_IMPORTABLE_SHEET"
    SHEET_LIMIT_EXCEEDED = "EXCEL_SHEET_LIMIT_EXCEEDED"
    ROW_LIMIT_EXCEEDED = "EXCEL_ROW_LIMIT_EXCEEDED"
    COLUMN_LIMIT_EXCEEDED = "EXCEL_COLUMN_LIMIT_EXCEEDED"
    CELL_LIMIT_EXCEEDED = "EXCEL_CELL_LIMIT_EXCEEDED"
    MERGED_CELL_UNSUPPORTED = "EXCEL_MERGED_CELL_UNSUPPORTED"
    FORMULA_UNSUPPORTED = "EXCEL_FORMULA_UNSUPPORTED"
    INVALID_HEADER = "EXCEL_INVALID_HEADER"
    DUPLICATE_HEADER = "EXCEL_DUPLICATE_HEADER"
    INVALID_TABLE_NAME = "EXCEL_INVALID_TABLE_NAME"
    DUPLICATE_TABLE_NAME = "EXCEL_DUPLICATE_TABLE_NAME"
    INVALID_CELL_VALUE = "EXCEL_INVALID_CELL_VALUE"
    MATERIALIZATION_FAILED = "EXCEL_MATERIALIZATION_FAILED"
    INTEGRITY_CHECK_FAILED = "EXCEL_INTEGRITY_CHECK_FAILED"
    DATASOURCE_CREATION_FAILED = "EXCEL_DATASOURCE_CREATION_FAILED"
    SCAN_FAILED = "EXCEL_SCAN_FAILED"


class ExcelIngestionError(QanerisError):
    """A deterministic ingestion failure.

    The message may name a sheet, a cell coordinate or a column position. It must never echo a
    stored cell value, a formula body or a resolved secret.
    """

    code = "excel_ingestion_failed"

    def __init__(
        self,
        error_code: ExcelErrorCode | str,
        message: str,
        *,
        sheet: str | None = None,
        coordinate: str | None = None,
    ):
        self.error_code = str(ExcelErrorCode(error_code))
        self.sheet = sheet
        self.coordinate = coordinate
        self.message = message
        # Skip ``QanerisError.__init__`` so the concrete code, not the family code, is raised.
        ValueError.__init__(self, f"{self.error_code}: {message}")


class ExcelImportStatus(StrEnum):
    """Lifecycle states recorded in ``manifest.json``."""

    MATERIALIZED = "MATERIALIZED"
    READY = "READY"
    FAILED = "FAILED"


class ExcelColumnSummary(BaseModel):
    source_header: str
    column_name: str
    inferred_type: str
    nullable: bool
    all_null: bool


class ExcelSheetSummary(BaseModel):
    original_name: str
    table_name: str
    row_count: int
    column_count: int
    columns: list[ExcelColumnSummary]


class ExcelImportRequest(BaseModel):
    """The single request an interface layer builds. No parser or materializer handle here."""

    file_path: str = Field(min_length=1)
    datasource_name: str | None = Field(default=None, min_length=1, max_length=100)
    workspace_id: str = Field(default="default", min_length=1)


class ExcelImportResult(BaseModel):
    """The outcome of one import.

    READY means every condition held: artifact, datasource, active ScanSnapshot and a verified
    Neo4j publication. ``neo4j_publication_verified`` is therefore always true on a returned
    result - a failure never produces a result, it raises and leaves a FAILED manifest instead, so
    ``status="READY"`` with ``neo4j_publication_verified=false`` cannot occur.
    """

    import_id: str
    workspace_id: str
    datasource_id: str
    snapshot_id: str
    scan_version: int
    status: str
    original_filename: str
    file_sha256: str
    sqlite_path: str
    artifact_directory: str
    manifest_path: str
    policy_version: str
    neo4j_publication_verified: bool
    sheets: list[ExcelSheetSummary] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ExcelManifest(BaseModel):
    """Lifecycle record stored next to the managed artifact.

    It records shape and type facts only: never business rows or cell values.
    """

    format_version: str = MANIFEST_FORMAT_VERSION
    policy_version: str
    import_id: str
    workspace_id: str
    original_filename: str
    file_sha256: str
    file_size: int
    created_at: str
    status: str
    sqlite_path: str | None = None
    datasource_id: str | None = None
    snapshot_id: str | None = None
    scan_version: int | None = None
    sheets: list[ExcelSheetSummary] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error_code: str | None = None
