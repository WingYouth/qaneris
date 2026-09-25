"""Application-facing Excel ingestion service (EXCEL-01A).

The service owns exactly three things: the managed artifact, idempotency, and delegation into the
existing secure datasource + initialization chain. Validation lives in
:mod:`qaneris.ingestion.excel.validator`, SQLite writing in
:mod:`qaneris.ingestion.excel.materializer`, and this module never writes Catalog datasets, fields
or relations and never writes Neo4j directly.

An import is READY only when all four hold: artifact READY, datasource READY, active ScanSnapshot
READY and the Neo4j publication verified. Publication is part of that contract, not a warning, so a
reader that cannot confirm the published graph fails the import instead of producing
``status=READY`` with ``neo4j_publication_verified=false``. Every failure below the container
preflight leaves a FAILED ``manifest.json`` behind, so an attempt that created an artifact is always
auditable.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from qaneris.common import artifacts
from qaneris.common.errors import GraphUnavailableError
from qaneris.common.redaction import safe_error
from qaneris.contracts.connection import (
    AuthenticationConfig,
    AuthenticationMethod,
    ConnectionEndpoint,
    ConnectionProfile,
    SecureDatasourceCreate,
)
from qaneris.contracts.datasource import DatasourceKind
from qaneris.contracts.profile import ScanStatus
from qaneris.graph.ports import NullGraphReader
from qaneris.graph.reading import GraphStructureRequest
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
from qaneris.ingestion.excel.materializer import SQLiteMaterializer, TableWrite
from qaneris.ingestion.excel.policy import (
    DEFAULT_EXCEL_POLICY,
    ExcelIngestionPolicy,
    artifact_key,
    policy_digest,
)
from qaneris.ingestion.excel.validator import (
    WorkbookPlan,
    iter_table_rows,
    read_container,
    validate_workbook,
)

if TYPE_CHECKING:  # pragma: no cover - import-cycle guard, typing only
    from qaneris.application.service import QanerisService

SOURCE_FILENAME = "source.xlsx"
SQLITE_FILENAME = "data.sqlite3"
MANIFEST_FILENAME = "manifest.json"

_DIRECTORY_MODE = 0o700
_ARTIFACT_MODE = 0o600


@dataclass(frozen=True)
class _Attempt:
    """Everything a failure record needs, resolved before any artifact is created."""

    import_id: str
    workspace_id: str
    original_filename: str
    file_sha256: str
    file_size: int
    created_at: str
    manifest_path: Path
    sqlite_path: Path


class ExcelIngestionService:
    def __init__(
        self,
        application: QanerisService,
        policy: ExcelIngestionPolicy | None = None,
        artifact_directory_root: Path | None = None,
        materializer: SQLiteMaterializer | None = None,
    ):
        self._application = application
        self.policy = policy or DEFAULT_EXCEL_POLICY
        self._artifact_directory_root = artifact_directory_root
        self._materializer = materializer or SQLiteMaterializer()

    # -- public entry point -----------------------------------------------------------------

    def import_excel(self, request: ExcelImportRequest) -> ExcelImportResult:
        data, original_filename = self._read_source(request.file_path)
        file_sha256 = hashlib.sha256(data).hexdigest()
        workspace_digest = _workspace_digest(request.workspace_id)
        policy_key = artifact_key(self.policy)
        import_id = f"excel_{workspace_digest}_{policy_digest(self.policy)}_{file_sha256[:16]}"
        directory = (
            self._root()
            / "ingestion"
            / "excel"
            / workspace_digest
            / policy_key
            / file_sha256
        )
        sqlite_path = directory / SQLITE_FILENAME

        existing = self._reusable_import(directory, request, file_sha256)
        if existing is not None:
            return existing

        # Container preflight runs before any artifact is created: a file that is not an accepted
        # .xlsx never reaches the disk, the SQLite writer or the datasource chain.
        read_container(data, original_filename, self.policy)

        previous = self._read_manifest(directory / MANIFEST_FILENAME)
        attempt = _Attempt(
            import_id=import_id,
            workspace_id=request.workspace_id,
            original_filename=original_filename,
            file_sha256=file_sha256,
            file_size=len(data),
            created_at=previous.created_at if previous is not None else _utc_now(),
            manifest_path=directory / MANIFEST_FILENAME,
            sqlite_path=sqlite_path,
        )
        self._prepare_directory(directory)
        self._write_source(directory / SOURCE_FILENAME, data)

        plan: WorkbookPlan | None = None
        try:
            plan = validate_workbook(data, self.policy)
            self._materialize(plan, sqlite_path)
        except ExcelIngestionError as error:
            self._record_failure(attempt, error, plan=plan)
            raise

        # The downstream stages are recorded too: a datasource, scan or publication failure must
        # leave the same auditable FAILED manifest that a validation failure leaves.
        datasource_id: str | None = None
        snapshot_id: str | None = None
        scan_version: int | None = None
        try:
            datasource_id, snapshot_id, scan_version = self._ensure_scan(
                request, sqlite_path, original_filename, retry=previous is not None
            )
            self._verify_publication(
                request.workspace_id, datasource_id, scan_version
            )
        except ExcelIngestionError as error:
            self._record_failure(
                attempt,
                error,
                plan=plan,
                datasource_id=datasource_id,
                snapshot_id=snapshot_id,
                scan_version=scan_version,
            )
            raise

        manifest = _manifest(
            status=ExcelImportStatus.READY,
            policy_version=self.policy.version,
            import_id=import_id,
            workspace_id=request.workspace_id,
            original_filename=original_filename,
            file_sha256=file_sha256,
            file_size=len(data),
            created_at=attempt.created_at,
            sqlite_path=sqlite_path,
            datasource_id=datasource_id,
            snapshot_id=snapshot_id,
            scan_version=scan_version,
            plan=plan,
        )
        self._write_manifest(attempt.manifest_path, manifest)
        return _result(manifest, directory)

    # -- managed artifact -------------------------------------------------------------------

    def _root(self) -> Path:
        if self._artifact_directory_root is not None:
            return Path(self._artifact_directory_root).expanduser().resolve()
        try:
            return artifacts.artifact_root()
        except ValueError as error:
            raise ExcelIngestionError(
                ExcelErrorCode.MATERIALIZATION_FAILED, f"受管产物目录不可用：{error}"
            ) from error

    @staticmethod
    def _read_source(file_path: str) -> tuple[bytes, str]:
        path = Path(file_path).expanduser()
        if not path.is_file():
            raise ExcelIngestionError(
                ExcelErrorCode.INVALID_CONTAINER, "待导入文件不存在或不是普通文件"
            )
        try:
            return path.read_bytes(), path.name
        except OSError as error:
            raise ExcelIngestionError(
                ExcelErrorCode.INVALID_CONTAINER, "待导入文件无法读取"
            ) from error

    def _prepare_directory(self, directory: Path) -> None:
        """Create the artifact directory private at every level down from the artifact root."""
        directory.mkdir(parents=True, exist_ok=True)
        root = self._root()
        target = directory
        while target != root and target.is_relative_to(root):
            os.chmod(target, _DIRECTORY_MODE)
            target = target.parent

    @staticmethod
    def _write_source(path: Path, data: bytes) -> None:
        if path.is_file() and _file_sha256(path) == hashlib.sha256(data).hexdigest():
            return
        _atomic_write(path, data)

    def _materialize(self, plan: WorkbookPlan, sqlite_path: Path) -> None:
        """Materialize once per artifact identity.

        The directory is content-addressed by workspace + policy + exact bytes, so an existing
        ``data.sqlite3`` can only come from a completed materialization of these very bytes under
        this very policy. Reusing it is what makes a retry after a downstream failure safe: the
        finalized, immutable artifact is never rewritten.
        """
        tables = _table_writes(plan)
        if self._materializer.is_finalized(
            sqlite_path, [table.table_name for table in tables]
        ):
            return
        self._materializer.materialize(tables, sqlite_path)

    @staticmethod
    def _read_manifest(path: Path) -> ExcelManifest | None:
        if not path.is_file():
            return None
        try:
            return ExcelManifest.model_validate(json.loads(path.read_text("utf-8")))
        except (OSError, ValueError):
            return None

    @staticmethod
    def _write_manifest(path: Path, manifest: ExcelManifest) -> None:
        payload = json.dumps(
            manifest.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True
        )
        _atomic_write(path, payload.encode("utf-8"))

    def _record_failure(
        self,
        attempt: _Attempt,
        error: ExcelIngestionError,
        *,
        plan: WorkbookPlan | None,
        datasource_id: str | None = None,
        snapshot_id: str | None = None,
        scan_version: int | None = None,
    ) -> None:
        """Keep an auditable FAILED manifest; a failure must never mask the ingestion error.

        Only ids the chain really produced are recorded - nothing is fabricated - and only the
        stable error code is stored, never a cell value. ``source.xlsx`` is left in place: it is a
        finalized artifact and a retry still needs it.
        """
        manifest = _manifest(
            status=ExcelImportStatus.FAILED,
            policy_version=self.policy.version,
            import_id=attempt.import_id,
            workspace_id=attempt.workspace_id,
            original_filename=attempt.original_filename,
            file_sha256=attempt.file_sha256,
            file_size=attempt.file_size,
            created_at=attempt.created_at,
            sqlite_path=attempt.sqlite_path if attempt.sqlite_path.is_file() else None,
            datasource_id=datasource_id,
            snapshot_id=snapshot_id,
            scan_version=scan_version,
            plan=plan,
            error_code=error.error_code,
        )
        try:
            self._write_manifest(attempt.manifest_path, manifest)
        except OSError:
            pass

    # -- idempotency ------------------------------------------------------------------------

    def _reusable_import(
        self, directory: Path, request: ExcelImportRequest, file_sha256: str
    ) -> ExcelImportResult | None:
        """Return the existing READY import for the same bytes, policy and workspace.

        The idempotency key is ``workspace_id + exact SHA-256 + policy_version``, and the artifact
        directory already encodes all three, so a different file or a different policy addresses a
        different directory and is built from its own bytes.

        A stored READY record is only reported after the publication is confirmed again. Note that
        this does *not* rewrite the record when the graph is temporarily unavailable: the artifact is
        finalized and its lifecycle record stays as it was, but the caller still never receives a
        READY result it cannot stand behind.
        """
        manifest = self._read_manifest(directory / MANIFEST_FILENAME)
        if manifest is None or manifest.status != ExcelImportStatus.READY.value:
            return None
        if manifest.file_sha256 != file_sha256 or manifest.policy_version != self.policy.version:
            return None
        if manifest.workspace_id != request.workspace_id or manifest.datasource_id is None:
            return None
        if not (directory / SQLITE_FILENAME).is_file():
            return None
        snapshot = self._active_snapshot(manifest.datasource_id)
        if snapshot is None or manifest.scan_version != snapshot.version:
            return None
        self._verify_publication(request.workspace_id, manifest.datasource_id, snapshot.version)
        return _result(manifest, directory)

    # -- existing Qaneris chain -----------------------------------------------------------

    def _ensure_scan(
        self,
        request: ExcelImportRequest,
        sqlite_path: Path,
        original_filename: str,
        *,
        retry: bool,
    ) -> tuple[str, str, int]:
        """Reach datasource READY + active ScanSnapshot READY through the existing chain.

        ``create_secure_datasource`` tests and saves only; ``Test → Save → Scan`` are three separate
        steps in the secure datasource contract, so the scan is requested explicitly here. A freshly
        created datasource is therefore always initialized, and the import keeps its full
        ``.xlsx → SQLite → secure datasource → initialize → snapshot → Neo4j → READY`` definition
        rather than relaxing it to whatever create happens to leave behind.

        ``retry`` means an earlier attempt for these exact bytes and this exact policy already
        created this artifact but never reached READY. The datasource and its snapshot may then look
        complete while the publication never happened, so the datasource is re-scanned and
        re-published instead of being trusted. The artifact itself is not touched.
        """
        datasource_id = self._existing_datasource_id(request.workspace_id, sqlite_path)
        if datasource_id is None:
            datasource_id = self._create_datasource(request, sqlite_path, original_filename)
            # A newly created datasource has never been scanned, so this is the explicit scan step.
            should_initialize = True
        else:
            should_initialize = retry or self._active_snapshot(datasource_id) is None
        if should_initialize:
            job = self._application.initialize_datasource(datasource_id)
            if job.status != ScanStatus.READY:
                raise ExcelIngestionError(
                    ExcelErrorCode.SCAN_FAILED,
                    f"数据源初始化未完成（{job.status.value}）",
                )
        snapshot = self._active_snapshot(datasource_id)
        if snapshot is None or snapshot.status != ScanStatus.READY:
            raise ExcelIngestionError(
                ExcelErrorCode.SCAN_FAILED, "未产生 READY 的活跃 ScanSnapshot"
            )
        if not self._is_ready(datasource_id):
            raise ExcelIngestionError(ExcelErrorCode.SCAN_FAILED, "数据源未进入 READY 状态")
        return datasource_id, snapshot.id, snapshot.version

    def _create_datasource(
        self, request: ExcelImportRequest, sqlite_path: Path, original_filename: str
    ) -> str:
        secure = SecureDatasourceCreate(
            name=_datasource_name(request, original_filename),
            kind=DatasourceKind.RELATIONAL,
            workspace_id=request.workspace_id,
            connection_profile=ConnectionProfile(
                driver="sqlite",
                endpoint=ConnectionEndpoint(path=str(sqlite_path)),
                authentication=AuthenticationConfig(method=AuthenticationMethod.NONE),
            ),
        )
        try:
            datasource = self._application.create_secure_datasource(secure)
        except ExcelIngestionError:
            raise
        except Exception as error:
            # Create only tests and saves, so a failure here is a datasource-creation failure. The
            # scan is a separate, explicit step whose failure is reported by its own job status.
            raise ExcelIngestionError(
                ExcelErrorCode.DATASOURCE_CREATION_FAILED, safe_error(error)
            ) from error
        return datasource.id

    def _catalog(self) -> Any:
        return self._application.catalog

    def _existing_datasource_id(self, workspace_id: str, sqlite_path: Path) -> str | None:
        """Find an already-registered datasource pointing at this managed SQLite file.

        This is the second idempotency guard: even without a READY manifest (for example after a
        crash between scan and manifest write) a retry reuses the existing datasource instead of
        creating a duplicate.
        """
        target = sqlite_path.resolve()
        try:
            datasources = self._catalog().list_datasources(workspace_id)
        except Exception:  # noqa: BLE001 - an unreadable catalog is reported by the create path
            return None
        for datasource in datasources:
            if datasource.driver != "sqlite":
                continue
            profile = self._catalog().get_connection_profile(datasource.id)
            if profile is None or not profile.endpoint.path:
                continue
            if Path(profile.endpoint.path).expanduser().resolve() == target:
                return datasource.id
        return None

    def _active_snapshot(self, datasource_id: str | None) -> Any:
        if not datasource_id:
            return None
        try:
            if not self._is_ready(datasource_id):
                return None
        except KeyError:
            return None
        return self._catalog().get_active_snapshot(datasource_id)

    def _is_ready(self, datasource_id: str) -> bool:
        datasource, _ = self._catalog().get_datasource(datasource_id)
        return datasource.status == "ready"

    def _verify_publication(
        self, workspace_id: str, datasource_id: str, scan_version: int
    ) -> None:
        """Confirm the published graph carries this datasource at the snapshot revision.

        Publication is part of the READY contract, so this fails closed. A missing or compatibility
        reader (``NullGraphReader``) cannot confirm anything, which is not the same as "published and
        correct": reporting READY there would be a claim the import cannot support. An unreachable
        graph stays a hard failure too, because "not published" and "published but empty" are
        different outcomes.
        """
        reader = getattr(self._application, "graph_reader", None)
        if reader is None or isinstance(reader, NullGraphReader):
            raise ExcelIngestionError(
                ExcelErrorCode.SCAN_FAILED,
                "Neo4j 图发布校验失败：未配置可验证的图读取后端，无法确认本次导入已发布",
            )
        try:
            structure = reader.read_structure(
                GraphStructureRequest(
                    workspace_id=workspace_id,
                    datasource_id=datasource_id,
                    max_data_objects=1,
                    max_fields=1,
                    max_relationships=1,
                )
            )
        except GraphUnavailableError as error:
            raise ExcelIngestionError(
                ExcelErrorCode.SCAN_FAILED, f"Neo4j 图发布校验失败：{error}"
            ) from error
        matches = [item for item in structure.datasources if item.datasource_id == datasource_id]
        if len(matches) != 1 or matches[0].scan_version != scan_version:
            raise ExcelIngestionError(
                ExcelErrorCode.SCAN_FAILED,
                "Neo4j 图发布校验失败：发布的数据源或 scan_version 与快照不一致",
            )


# --------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------


def _workspace_digest(workspace_id: str) -> str:
    return hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()[:12]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _datasource_name(request: ExcelImportRequest, original_filename: str) -> str:
    candidate = (request.datasource_name or f"Excel: {original_filename}").strip()
    return (candidate or "Excel import")[:100]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    """Write one artifact through a private, unpredictably named temporary file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}-{uuid.uuid4().hex}.tmp"
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, _ARTIFACT_MODE)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _table_writes(plan: WorkbookPlan) -> list[TableWrite]:
    return [
        TableWrite(
            table_name=sheet.table_name,
            columns=sheet.columns,
            row_count=sheet.row_count,
            rows=(lambda selected=sheet: iter_table_rows(plan, selected)),
        )
        for sheet in plan.sheets
    ]


def _sheet_summaries(plan: WorkbookPlan | None) -> list[ExcelSheetSummary]:
    if plan is None:
        return []
    return [
        ExcelSheetSummary(
            original_name=sheet.original_name,
            table_name=sheet.table_name,
            row_count=sheet.row_count,
            column_count=len(sheet.columns),
            columns=[
                ExcelColumnSummary(
                    source_header=column.source_header,
                    column_name=column.column_name,
                    inferred_type=column.inferred_type.value,
                    nullable=column.nullable,
                    all_null=column.all_null,
                )
                for column in sheet.columns
            ],
        )
        for sheet in plan.sheets
    ]


def _manifest(
    *,
    status: ExcelImportStatus,
    policy_version: str,
    import_id: str,
    workspace_id: str,
    original_filename: str,
    file_sha256: str,
    file_size: int,
    created_at: str,
    plan: WorkbookPlan | None,
    sqlite_path: Path | None = None,
    datasource_id: str | None = None,
    snapshot_id: str | None = None,
    scan_version: int | None = None,
    error_code: str | None = None,
) -> ExcelManifest:
    return ExcelManifest(
        format_version=MANIFEST_FORMAT_VERSION,
        policy_version=policy_version,
        import_id=import_id,
        workspace_id=workspace_id,
        original_filename=original_filename,
        file_sha256=file_sha256,
        file_size=file_size,
        created_at=created_at,
        status=status.value,
        sqlite_path=str(sqlite_path) if sqlite_path is not None else None,
        datasource_id=datasource_id,
        snapshot_id=snapshot_id,
        scan_version=scan_version,
        sheets=_sheet_summaries(plan),
        warnings=list(plan.warnings) if plan is not None else [],
        error_code=error_code,
    )


def _result(manifest: ExcelManifest, directory: Path) -> ExcelImportResult:
    """Build the only result shape the service returns.

    A result is only ever built once every READY condition has been observed - the publication
    verification included - so ``neo4j_publication_verified`` is true by construction here. A
    failure never reaches this function: it raises and leaves a FAILED manifest instead.
    """
    return ExcelImportResult(
        import_id=manifest.import_id,
        workspace_id=manifest.workspace_id,
        datasource_id=manifest.datasource_id or "",
        snapshot_id=manifest.snapshot_id or "",
        scan_version=manifest.scan_version or 0,
        status=manifest.status,
        original_filename=manifest.original_filename,
        file_sha256=manifest.file_sha256,
        sqlite_path=manifest.sqlite_path or str(directory / SQLITE_FILENAME),
        artifact_directory=str(directory),
        manifest_path=str(directory / MANIFEST_FILENAME),
        policy_version=manifest.policy_version,
        neo4j_publication_verified=True,
        sheets=list(manifest.sheets),
        warnings=list(manifest.warnings),
    )
