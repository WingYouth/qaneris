"""HTTP boundary for browser Excel upload (EXCEL-01C).

A browser can only hand over bytes, so this module owns exactly three things the Core must not
have to know about:

1. **Staging.** The multipart body is streamed into a private, unpredictably named directory outside
   the repository and deleted again in a ``finally`` block, whatever the outcome. The upload is not
   a managed artifact - the Core still owns ``source.xlsx`` / ``data.sqlite3`` / ``manifest.json``.
2. **Resource protection.** The stream is bounded by ``ExcelIngestionPolicy.max_file_size_bytes``
   while it is being read, so an oversized body is refused instead of being buffered in full. This
   is an early guard only: the Core keeps its own validation, and nothing here replaces it.
3. **Product projection.** The response only ever carries product-safe fields. ``ExcelImportResult``
   knows internal locations (``sqlite_path`` / ``artifact_directory`` / ``manifest_path``); none of
   them, and nothing about the temporary upload, reaches the browser.

The endpoint calls ``SmartDataService.import_excel()`` and nothing else. It never touches the
validator, the materializer, the catalog or the graph.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import Request, UploadFile
from fastapi.responses import JSONResponse

from smartdata.application.service import SmartDataService
from smartdata.common import artifacts
from smartdata.common.errors import DatasourceUnavailableError
from smartdata.common.redaction import SecretRedactor
from smartdata.contracts import ErrorDetail, ErrorResponse
from smartdata.ingestion.excel import (
    DEFAULT_EXCEL_POLICY,
    ExcelErrorCode,
    ExcelImportResult,
    ExcelIngestionError,
)

#: Capability segment for the private staging root under the managed artifact root.
STAGING_CAPABILITY = "uploads"

#: Read granularity of the multipart stream. The bound is enforced per chunk, so the body is never
#: materialized in memory as a whole.
STREAM_CHUNK_BYTES = 1024 * 1024

#: Mirrors ``ExcelImportRequest.datasource_name``. A value outside it is a request error, not an
#: ingestion failure, so it is rejected before the Core is entered.
DATASOURCE_NAME_LIMIT = 100

#: Longest accepted upload filename. Well beyond any real workbook name, small enough that the name
#: can never be a filesystem problem.
_FILENAME_LIMIT = 200

_STAGING_DIRECTORY_MODE = 0o700
_STAGED_FILE_MODE = 0o600

#: The projection the browser is allowed to see. ``artifact_directory`` / ``sqlite_path`` /
#: ``manifest_path`` are deliberately absent.
PRODUCT_FIELDS = (
    "status",
    "import_id",
    "workspace_id",
    "datasource_id",
    "snapshot_id",
    "scan_version",
    "original_filename",
    "file_sha256",
    "policy_version",
    "neo4j_publication_verified",
    "sheets",
    "warnings",
)

#: An oversized body is the one refusal with its own transport status.
_TOO_LARGE_STATUS = 413
_REFUSED_STATUS = 400


@dataclass(frozen=True)
class StagedUpload:
    """One temporary upload: a path inside a private directory, plus its safe display name."""

    path: Path
    directory: Path
    filename: str


def staging_root() -> Path:
    """Create the private staging root under the managed artifact root (outside the repository)."""
    try:
        root = artifacts.artifact_directory(STAGING_CAPABILITY)
    except ValueError as error:
        raise DatasourceUnavailableError(f"受管上传目录不可用：{error}") from error
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, _STAGING_DIRECTORY_MODE)
    return root


def safe_upload_filename(raw: str | None) -> str:
    """Reduce a client-supplied filename to one safe path segment.

    The staged file keeps the user's filename because the Core derives ``original_filename`` from
    the path it is given, and that name is product provenance. It is safe to keep because only the
    final segment is used, control characters and NUL are refused, and the segment is verified to
    be a single component - ``../`` can therefore never escape the private directory. The directory
    itself is unpredictably named and unreadable to anyone else.
    """
    candidate = (raw or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not candidate or candidate in {".", ".."} or any(
        character == "\0" or ord(character) < 32 for character in candidate
    ):
        raise ExcelIngestionError(
            ExcelErrorCode.INVALID_EXTENSION, "上传文件名缺失或不可用"
        )
    if len(candidate) > _FILENAME_LIMIT:
        candidate = candidate[-_FILENAME_LIMIT:]
    if Path(candidate).name != candidate:
        raise ExcelIngestionError(
            ExcelErrorCode.INVALID_EXTENSION, "上传文件名包含路径分隔符"
        )
    return candidate


@asynccontextmanager
async def staged_upload(
    upload: UploadFile,
    *,
    max_bytes: int | None = None,
    root: Path | None = None,
) -> AsyncIterator[StagedUpload]:
    """Stream one multipart part into private staging and always clean it up.

    The limit is the Core's own ``max_file_size_bytes``, never a second copy of the number, and it
    is checked while reading: a body one byte over the bound is refused before the rest of it is
    read, and nothing is left on disk afterwards.
    """
    limit = max_bytes if max_bytes is not None else DEFAULT_EXCEL_POLICY.max_file_size_bytes
    filename = safe_upload_filename(upload.filename)
    directory = Path(tempfile.mkdtemp(prefix="excel-upload-", dir=root or staging_root()))
    path = directory / filename
    descriptor: int | None = None
    written = 0
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, _STAGED_FILE_MODE)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            while True:
                chunk = await upload.read(STREAM_CHUNK_BYTES)
                if not chunk:
                    break
                written += len(chunk)
                if written > limit:
                    raise ExcelIngestionError(
                        ExcelErrorCode.FILE_TOO_LARGE,
                        f"上传文件超过 Excel 导入大小限制（最大 {limit} 字节）",
                    )
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        yield StagedUpload(path=path, directory=directory, filename=filename)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        _remove_tree(directory)


def _remove_tree(directory: Path) -> None:
    """Delete the private staging directory, whatever was written into it."""
    shutil.rmtree(directory, ignore_errors=True)


def import_payload(result: ExcelImportResult) -> dict[str, object]:
    """The product-safe projection of one READY import.

    A stable field selection, not a second source of truth: every value comes from
    ``ExcelImportResult``. The managed artifact location stays server-side.
    """
    dumped = result.model_dump(mode="json")
    return {name: dumped[name] for name in PRODUCT_FIELDS}


def excel_error_response(error: ExcelIngestionError) -> JSONResponse:
    """Keep the stable Excel code, and only the facts a browser may see.

    The message may name a sheet and a cell coordinate; it never carries a formula body, a stored
    cell value or a resolved credential. A generic ``SmartDataError`` handler would flatten every
    one of these into one code, which is exactly what a caller must be able to branch on.
    """
    payload = ErrorResponse(
        error=ErrorDetail(
            code=error.error_code,
            message=SecretRedactor.from_environment().text(error.message),
            sheet=error.sheet,
            coordinate=error.coordinate,
        )
    )
    return JSONResponse(
        status_code=_status_for(error.error_code), content=payload.model_dump()
    )


def _status_for(error_code: str) -> int:
    """One refusal, one transport status; the stable code carries the meaning."""
    if error_code == ExcelErrorCode.FILE_TOO_LARGE.value:
        return _TOO_LARGE_STATUS
    return _REFUSED_STATUS


def validate_import_fields(name: str | None, workspace_id: str) -> None:
    """Reject unusable form fields as a request error, before the Core is entered."""
    if name is not None:
        stripped = name.strip()
        if not stripped or len(stripped) > DATASOURCE_NAME_LIMIT:
            raise ValueError(
                f"name 必须是 1 到 {DATASOURCE_NAME_LIMIT} 个字符之间的非空字符串"
            )
    if not workspace_id.strip():
        raise ValueError("workspace_id 不能为空")


def service_of(request: Request) -> SmartDataService:
    """Resolve the single application service the process was composed with."""
    service = getattr(request.app.state, "service", None)
    if service is None:  # pragma: no cover - a miscomposed app, not a user-reachable state
        raise DatasourceUnavailableError("应用服务未初始化")
    return service
