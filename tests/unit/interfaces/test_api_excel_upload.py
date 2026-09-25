"""HTTP Excel upload boundary tests (EXCEL-01C).

These tests cover the interface layer only - the Core's own 117 ingestion cases already live in
``tests/unit/ingestion/test_excel_ingestion.py`` and are not repeated here. What is locked here is
what the browser boundary adds: exactly one call into ``QanerisService.import_excel()``, a
temporary upload that never outlives the request, a response that never names an internal location,
and refusals that keep their stable Excel code.

Most negatives run the *real* Core through the *real* endpoint: container preflight, workbook
validation and datasource creation all happen before anything needs a live graph, so extension,
container, formula and publication refusals are exercised end to end. The success path runs the real
Core against a graph backend that records the publication, so the response projection is asserted on
a real ``ExcelImportResult`` rather than on a hand-built dictionary.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import stat
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from qaneris.application.service import QanerisService
from qaneris.catalog import Catalog
from qaneris.common.errors import GraphUnavailableError
from qaneris.graph import GraphDatasource, GraphStructure, GraphStructureRequest
from qaneris.ingestion.excel import (
    DEFAULT_EXCEL_POLICY,
    ExcelImportRequest,
    ExcelImportResult,
    ExcelIngestionError,
)
from qaneris.interfaces.api import excel as excel_boundary
from qaneris.interfaces.api.app import create_app

ORDERS: list[list[Any]] = [
    ["order_id", "order_date", "region", "amount", "status"],
    ["E1", "2026-01-05", "East", 100.25, "completed"],
    ["E2", "2026-01-06", "West", 120.5, "completed"],
]

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

#: The internal locations ``ExcelImportResult`` carries and the browser must never be told.
INTERNAL_FIELDS = ("sqlite_path", "artifact_directory", "manifest_path")

ENDPOINT = "/api/datasources/import-excel"


class FakeGraph:
    """Records what ``DatabaseInitializer`` published and answers the publication check."""

    def __init__(self) -> None:
        self.published: dict[tuple[str, str], int] = {}

    def ensure_schema(self) -> None:
        return None

    def replace_datasource_graph(self, graph: Any) -> None:
        self.published[(graph.workspace_id, graph.datasource_id)] = graph.scan_version

    def read_structure(self, request: GraphStructureRequest) -> GraphStructure:
        return GraphStructure(
            datasources=[
                GraphDatasource(
                    node_id=f"node::{datasource_id}",
                    datasource_id=datasource_id,
                    workspace_id=workspace,
                    name=datasource_id,
                    scan_version=version,
                )
                for (workspace, datasource_id), version in sorted(self.published.items())
                if workspace == request.workspace_id
                and (request.datasource_id is None or datasource_id == request.datasource_id)
            ]
        )


class UnavailableGraph(FakeGraph):
    """A reader that cannot answer, so the publication cannot be confirmed."""

    def read_structure(self, request: GraphStructureRequest) -> GraphStructure:
        raise GraphUnavailableError("simulated Neo4j outage")


class RecordingService:
    """A service double that records the one call the boundary is allowed to make.

    It also captures the staged path and its mode *while the call is in flight*, which is the only
    moment a temporary upload is supposed to exist.
    """

    def __init__(self, *, result: ExcelImportResult | None = None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls: list[ExcelImportRequest] = []
        self.staged_during_call: list[tuple[Path, int]] = []

    def import_excel(self, request: ExcelImportRequest) -> ExcelImportResult:
        staged = Path(request.file_path)
        self.calls.append(request)
        # Captured while the call is in flight - the only moment the temporary upload may exist.
        self.staged_during_call.append(
            (staged, stat.S_IMODE(staged.stat().st_mode), staged.is_file())
        )
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


# --------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def artifact_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep both the managed artifact root and the staging root inside the test."""
    monkeypatch.setenv("QANERIS_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    return tmp_path / "artifacts"


def workbook_bytes(rows: list[list[Any]] = ORDERS, *, title: str = "orders") -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = title
    for row in rows:
        worksheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def formula_workbook_bytes() -> bytes:
    return workbook_bytes([["order_id", "amount"], [1, 100.0], [2, "=SUM(B2:B2)"]], title="orders")


def build_client(
    tmp_path: Path, service: Any = None, *, raise_server_exceptions: bool = True
) -> TestClient:
    """An app composed exactly like production, with its artifact root kept inside the test."""
    app = create_app(database_path=str(tmp_path / "catalog.db"))
    if service is not None:
        app.state.service = service
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def staging_root(tmp_path: Path) -> Path:
    return tmp_path / "artifacts" / excel_boundary.STAGING_CAPABILITY


def staged_leftovers(tmp_path: Path) -> list[Path]:
    root = staging_root(tmp_path)
    return list(root.iterdir()) if root.is_dir() else []


def post_excel(client: TestClient, data: bytes, filename: str = "orders.xlsx", **form: str):
    return client.post(
        ENDPOINT, files={"file": (filename, data, XLSX_MEDIA_TYPE)}, data=form
    )


def ready_result(tmp_path: Path) -> ExcelImportResult:
    """A real READY result: the real Core against a graph backend that records the publication."""
    graph = FakeGraph()
    service = QanerisService(
        Catalog(tmp_path / "catalog.db"), graph_store=graph, graph_reader=graph
    )
    source = tmp_path / "seed.xlsx"
    source.write_bytes(workbook_bytes())
    return service.import_excel(ExcelImportRequest(file_path=str(source)))


def composed_service(tmp_path: Path, reader: Any = None) -> QanerisService:
    graph = FakeGraph()
    return QanerisService(
        Catalog(tmp_path / "catalog.db"), graph_store=graph, graph_reader=reader or graph
    )


# --------------------------------------------------------------------------------------------
# happy path
# --------------------------------------------------------------------------------------------


def test_upload_returns_a_product_safe_ready_projection(tmp_path) -> None:
    client = build_client(tmp_path, composed_service(tmp_path))
    payload_bytes = workbook_bytes()

    response = post_excel(client, payload_bytes, name="Roadshow Orders", workspace_id="default")

    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "READY"
    assert payload["neo4j_publication_verified"] is True
    assert payload["datasource_id"]
    assert payload["snapshot_id"]
    assert payload["scan_version"] == 1
    assert payload["original_filename"] == "orders.xlsx"
    assert payload["workspace_id"] == "default"
    assert payload["policy_version"] == DEFAULT_EXCEL_POLICY.version
    assert payload["file_sha256"] == hashlib.sha256(payload_bytes).hexdigest()
    assert [sheet["table_name"] for sheet in payload["sheets"]] == ["orders"]
    assert payload["sheets"][0]["row_count"] == 2

    listed = client.get("/api/datasources").json()
    assert [item["id"] for item in listed] == [payload["datasource_id"]]
    assert listed[0]["status"] == "ready"
    assert listed[0]["name"] == "Roadshow Orders"


def test_response_never_exposes_internal_locations_or_the_upload_path(tmp_path) -> None:
    client = build_client(tmp_path, RecordingService(result=ready_result(tmp_path)))

    response = post_excel(client, workbook_bytes())

    assert response.status_code == 201
    body = response.text
    for field in INTERNAL_FIELDS:
        assert field not in body
    assert "data.sqlite3" not in body
    assert "manifest.json" not in body
    assert "source.xlsx" not in body
    assert str(tmp_path) not in body
    assert excel_boundary.STAGING_CAPABILITY not in body


def test_import_excel_is_the_only_service_call_and_receives_the_form_fields(tmp_path) -> None:
    recorder = RecordingService(result=ready_result(tmp_path))
    client = build_client(tmp_path, recorder)

    response = post_excel(
        client, workbook_bytes(), name="  Roadshow Orders  ", workspace_id="roadshow"
    )

    assert response.status_code == 201
    assert len(recorder.calls) == 1
    request = recorder.calls[0]
    assert request.datasource_name == "Roadshow Orders"
    assert request.workspace_id == "roadshow"
    # The request the boundary builds carries exactly the three product fields: no server path
    # supplied by the caller, no force/skip switch, no graph or scan knob.
    assert set(request.model_dump()) == {"file_path", "datasource_name", "workspace_id"}


def test_workspace_defaults_to_default_and_name_stays_absent(tmp_path) -> None:
    recorder = RecordingService(result=ready_result(tmp_path))
    client = build_client(tmp_path, recorder)

    assert post_excel(client, workbook_bytes()).status_code == 201
    assert recorder.calls[0].workspace_id == "default"
    assert recorder.calls[0].datasource_name is None


def test_staged_upload_is_private_outside_the_repository_and_removed_afterwards(tmp_path) -> None:
    recorder = RecordingService(result=ready_result(tmp_path))
    client = build_client(tmp_path, recorder)

    assert post_excel(client, workbook_bytes()).status_code == 201

    ((staged, mode, existed),) = recorder.staged_during_call
    assert existed is True
    assert mode == 0o600
    assert staged.parent.parent == staging_root(tmp_path)
    assert not staged.exists()
    assert staged_leftovers(tmp_path) == []


def test_staged_upload_is_removed_when_ingestion_refuses_the_file(tmp_path) -> None:
    recorder = RecordingService(
        error=ExcelIngestionError("EXCEL_FORMULA_UNSUPPORTED", "公式不受支持")
    )
    client = build_client(tmp_path, recorder)

    response = post_excel(client, formula_workbook_bytes())

    assert response.status_code == 400
    ((staged, _, existed),) = recorder.staged_during_call
    assert existed is True
    assert not staged.exists()
    assert staged_leftovers(tmp_path) == []


def test_staging_root_is_private(tmp_path) -> None:
    client = build_client(tmp_path, RecordingService(result=ready_result(tmp_path)))

    assert post_excel(client, workbook_bytes()).status_code == 201
    assert stat.S_IMODE(os.stat(staging_root(tmp_path)).st_mode) == 0o700


def test_upload_filename_cannot_escape_the_staging_directory(tmp_path) -> None:
    recorder = RecordingService(result=ready_result(tmp_path))
    client = build_client(tmp_path, recorder)

    response = post_excel(client, workbook_bytes(), filename="../../etc/evil.xlsx")

    assert response.status_code == 201
    (staged, _, _) = recorder.staged_during_call[0]
    assert staged.name == "evil.xlsx"
    assert staged.parent.parent == staging_root(tmp_path)


def test_upload_without_a_filename_part_is_rejected(tmp_path) -> None:
    recorder = RecordingService(result=ready_result(tmp_path))
    client = build_client(tmp_path, recorder)

    response = post_excel(client, workbook_bytes(), filename="")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert recorder.calls == []
    assert staged_leftovers(tmp_path) == []


def test_unusable_upload_filename_is_refused_before_staging(tmp_path) -> None:
    recorder = RecordingService(result=ready_result(tmp_path))
    client = build_client(tmp_path, recorder)

    response = post_excel(client, workbook_bytes(), filename="..")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "EXCEL_INVALID_EXTENSION"
    assert recorder.calls == []
    assert staged_leftovers(tmp_path) == []


# --------------------------------------------------------------------------------------------
# refusals through the real Core
# --------------------------------------------------------------------------------------------


def test_invalid_extension_keeps_its_stable_code(tmp_path) -> None:
    client = build_client(tmp_path)

    response = client.post(ENDPOINT, files={"file": ("notes.txt", b"not a workbook", "text/plain")})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "EXCEL_INVALID_EXTENSION"


def test_corrupt_container_keeps_its_stable_code(tmp_path) -> None:
    client = build_client(tmp_path)

    response = post_excel(client, b"PK\x03\x04not really a workbook")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "EXCEL_INVALID_CONTAINER"


def test_formula_workbook_keeps_its_code_location_and_hides_the_formula_body(tmp_path) -> None:
    client = build_client(tmp_path)

    response = post_excel(client, formula_workbook_bytes())

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "EXCEL_FORMULA_UNSUPPORTED"
    assert error["sheet"] == "orders"
    assert error["coordinate"] == "B3"
    assert "SUM" not in response.text


def test_refused_upload_creates_no_datasource_and_no_materialized_sqlite(tmp_path) -> None:
    client = build_client(tmp_path)

    assert post_excel(client, formula_workbook_bytes()).status_code == 400

    assert client.get("/api/datasources").json() == []
    assert list((tmp_path / "artifacts").rglob("data.sqlite3")) == []
    assert staged_leftovers(tmp_path) == []


def test_unknown_form_fields_cannot_skip_validation_or_supply_a_server_path(tmp_path) -> None:
    client = build_client(tmp_path)

    response = post_excel(
        client,
        formula_workbook_bytes(),
        skip_validation="true",
        no_scan="true",
        no_neo4j="true",
        file_path="/etc/passwd",
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "EXCEL_FORMULA_UNSUPPORTED"


@pytest.mark.parametrize(
    "form",
    [
        {"name": "   "},
        {"name": "x" * (excel_boundary.DATASOURCE_NAME_LIMIT + 1)},
        {"workspace_id": "   "},
    ],
)
def test_unusable_form_fields_are_rejected_before_the_core(tmp_path, form) -> None:
    recorder = RecordingService(result=ready_result(tmp_path))
    client = build_client(tmp_path, recorder)

    response = post_excel(client, workbook_bytes(), **form)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert recorder.calls == []
    assert staged_leftovers(tmp_path) == []


def test_missing_file_part_is_rejected(tmp_path) -> None:
    client = build_client(tmp_path)

    response = client.post(ENDPOINT, data={"workspace_id": "default"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_unverifiable_publication_is_not_ready_and_leaves_no_upload(tmp_path) -> None:
    client = build_client(tmp_path, composed_service(tmp_path, reader=UnavailableGraph()))

    response = post_excel(client, workbook_bytes())

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "EXCEL_SCAN_FAILED"
    assert staged_leftovers(tmp_path) == []


# --------------------------------------------------------------------------------------------
# resource protection
# --------------------------------------------------------------------------------------------


def test_oversized_upload_is_refused_at_the_boundary_with_the_policy_limit(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        excel_boundary,
        "DEFAULT_EXCEL_POLICY",
        replace(DEFAULT_EXCEL_POLICY, max_file_size_bytes=512),
    )
    recorder = RecordingService(result=ready_result(tmp_path))
    client = build_client(tmp_path, recorder)

    response = post_excel(client, b"x" * 4096)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "EXCEL_FILE_TOO_LARGE"
    assert "512" in response.json()["error"]["message"]
    assert recorder.calls == []
    assert staged_leftovers(tmp_path) == []


def test_upload_within_the_policy_limit_reaches_the_core(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        excel_boundary,
        "DEFAULT_EXCEL_POLICY",
        replace(DEFAULT_EXCEL_POLICY, max_file_size_bytes=1 << 20),
    )
    recorder = RecordingService(result=ready_result(tmp_path))
    client = build_client(tmp_path, recorder)

    assert post_excel(client, workbook_bytes()).status_code == 201
    assert len(recorder.calls) == 1


# --------------------------------------------------------------------------------------------
# unexpected failures
# --------------------------------------------------------------------------------------------


def test_unexpected_failure_is_redacted(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QANERIS_PROBE_CREDENTIAL", "top-secret-value")
    recorder = RecordingService(error=RuntimeError("unexpected top-secret-value"))
    client = build_client(tmp_path, recorder, raise_server_exceptions=False)

    response = post_excel(client, workbook_bytes())

    assert response.status_code == 500
    assert "top-secret-value" not in response.text
    assert staged_leftovers(tmp_path) == []


# --------------------------------------------------------------------------------------------
# the event loop
# --------------------------------------------------------------------------------------------


#: How long the stand-in import blocks for. It is bounded so that a regression fails the assertion
#: below with a real number instead of hanging the suite.
_BLOCKING_IMPORT_SECONDS = 5.0


def test_a_blocking_import_does_not_stall_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reading the body is asynchronous; the import itself is not, so it must not run on the loop.

    Scanning and publishing to the graph is a long synchronous call. If it ran on the event loop,
    every other request - ``/health`` included - would wait behind it, and one slow upload would take
    the whole server down with it. The import therefore runs in a worker thread, and this test holds
    it there while a second request is served.
    """
    release = threading.Event()

    class BlockingService:
        """A service whose import parks until the test releases it."""

        def import_excel(self, request: ExcelImportRequest) -> ExcelImportResult:
            release.wait(_BLOCKING_IMPORT_SECONDS)
            return ready_result(tmp_path)

    app = create_app(database_path=str(tmp_path / "catalog.db"))
    app.state.service = BlockingService()

    async def scenario() -> tuple[int, int, float]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            began = time.monotonic()
            uploading = asyncio.create_task(
                client.post(
                    ENDPOINT,
                    files={"file": ("orders.xlsx", workbook_bytes(), XLSX_MEDIA_TYPE)},
                )
            )
            # Let the upload reach the blocking Core call before asking for anything else.
            await asyncio.sleep(0)
            health = await client.get("/health")
            elapsed = time.monotonic() - began
            release.set()
            return health.status_code, (await uploading).status_code, elapsed

    health_status, upload_status, elapsed = asyncio.run(scenario())

    assert health_status == 200
    assert upload_status == 201
    assert elapsed < 1.0, f"a blocking import held the event loop for {elapsed:.2f}s"
