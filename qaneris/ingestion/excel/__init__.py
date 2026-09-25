"""EXCEL-01A core Excel ingestion: ``.xlsx`` → immutable SQLite → existing Qaneris chain.

Excel is an ingestion source, not a second query engine. The only supported route is::

    .xlsx bytes
    → deterministic validation
    → managed artifact (outside the repository)
    → immutable SQLite (read-only afterwards)
    → SecureDatasourceCreate
    → DatabaseInitializer (scan → ScanSnapshot → Neo4j publication)

Public surface: :class:`ExcelIngestionService`, :class:`ExcelImportRequest`,
:class:`ExcelImportResult`, :class:`ExcelIngestionError` and :class:`ExcelIngestionPolicy`.
Interface layers go through ``QanerisService.import_excel`` and never touch the validator, the
materializer or the artifact layout directly.
"""

from qaneris.ingestion.excel.contracts import (
    MANIFEST_FORMAT_VERSION,
    ExcelColumnSummary,
    ExcelErrorCode,
    ExcelImportRequest,
    ExcelImportResult,
    ExcelImportStatus,
    ExcelIngestionError,
    ExcelManifest,
    ExcelSheetSummary,
)
from qaneris.ingestion.excel.materializer import (
    SQLiteMaterializer,
    TableWrite,
    quote_identifier,
)
from qaneris.ingestion.excel.policy import (
    DEFAULT_EXCEL_POLICY,
    ExcelIngestionPolicy,
    artifact_key,
    policy_digest,
)
from qaneris.ingestion.excel.service import ExcelIngestionService
from qaneris.ingestion.excel.types import (
    CellKind,
    ColumnType,
    UnsupportedCellValue,
    classify_cell,
    coerce_cell,
    infer_column_type,
    widen,
)
from qaneris.ingestion.excel.validator import (
    ColumnPlan,
    ContainerInfo,
    SheetPlan,
    WorkbookPlan,
    iter_table_rows,
    read_container,
    validate_workbook,
)

__all__ = [
    "DEFAULT_EXCEL_POLICY",
    "MANIFEST_FORMAT_VERSION",
    "CellKind",
    "ColumnPlan",
    "ColumnType",
    "ContainerInfo",
    "ExcelColumnSummary",
    "ExcelErrorCode",
    "ExcelImportRequest",
    "ExcelImportResult",
    "ExcelImportStatus",
    "ExcelIngestionError",
    "ExcelIngestionPolicy",
    "ExcelIngestionService",
    "ExcelManifest",
    "ExcelSheetSummary",
    "SQLiteMaterializer",
    "SheetPlan",
    "TableWrite",
    "UnsupportedCellValue",
    "WorkbookPlan",
    "artifact_key",
    "classify_cell",
    "coerce_cell",
    "infer_column_type",
    "iter_table_rows",
    "policy_digest",
    "quote_identifier",
    "read_container",
    "validate_workbook",
    "widen",
]
