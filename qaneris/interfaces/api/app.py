"""HTTP API entry point."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import uvicorn
from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from qaneris.application.service import QanerisService
from qaneris.catalog import Catalog
from qaneris.common.errors import QanerisError
from qaneris.contracts import (
    AskRequest,
    DatasourceCreate,
    ErrorDetail,
    ErrorResponse,
    MappingInfo,
    RelationInfo,
)
from qaneris.ingestion.excel import ExcelImportRequest, ExcelIngestionError
from qaneris.interfaces.api.ask_stream import SSE_HEADERS, stream_ask_events
from qaneris.interfaces.api.conversations import register_conversation_routes
from qaneris.interfaces.api.credentials import register_credential_routes
from qaneris.interfaces.api.datasources import register_datasource_routes
from qaneris.interfaces.api.excel import (
    excel_error_response,
    import_payload,
    service_of,
    staged_upload,
    validate_import_fields,
)
from qaneris.llm.gateway import summarize_without_model
from qaneris.querying.retrieval.schema_context import build_schema_context


def create_app(database_path: str | None = None) -> FastAPI:
    path = database_path or os.getenv("QANERIS_CATALOG", "qaneris.db")
    service = QanerisService(Catalog(path))
    app = FastAPI(title="Qaneris API", version="0.1.0")
    app.state.service = service
    web_dir = Path(__file__).resolve().parents[3] / "web"
    app.mount("/static", StaticFiles(directory=web_dir), name="static")

    def error_response(code: str, message: str, status_code: int) -> JSONResponse:
        payload = ErrorResponse(error=ErrorDetail(code=code, message=message))
        return JSONResponse(status_code=status_code, content=payload.model_dump())

    @app.exception_handler(QanerisError)
    async def qaneris_error_handler(_: Request, error: QanerisError) -> JSONResponse:
        return error_response(error.code, error.message, error.status_code)

    @app.exception_handler(KeyError)
    async def not_found_error_handler(_: Request, error: KeyError) -> JSONResponse:
        message = str(error.args[0]) if error.args else "资源不存在"
        return error_response("resource_not_found", message, 404)

    @app.exception_handler(NotImplementedError)
    async def unsupported_error_handler(_: Request, error: NotImplementedError) -> JSONResponse:
        return error_response("unsupported_feature", str(error), 400)

    @app.exception_handler(ValueError)
    async def invalid_request_handler(_: Request, error: ValueError) -> JSONResponse:
        return error_response("invalid_request", str(error), 400)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, error: RequestValidationError) -> JSONResponse:
        first_error = error.errors()[0] if error.errors() else {}
        message = str(first_error.get("msg", "请求参数校验失败"))
        return error_response("validation_error", message, 422)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": "0.1.0"}

    @app.get("/api/datasources")
    def list_datasources(workspace_id: str = Query("default")):
        return service.list_datasources(workspace_id)

    @app.get("/api/adapters")
    def list_adapters():
        return service.list_supported_adapters()

    @app.post("/api/datasources", status_code=201)
    def create_datasource(request: DatasourceCreate):
        return service.create_datasource(request)

    @app.post("/api/datasources/import-excel", status_code=201)
    async def import_excel(
        request: Request,
        file: Annotated[UploadFile, File()],
        name: Annotated[str | None, Form()] = None,
        workspace_id: Annotated[str, Form()] = "default",
    ):
        """Import an uploaded ``.xlsx`` workbook as a first-class datasource.

        The browser submits bytes only - no server path, no validation switch and no way to skip the
        scan or the publication check. The upload is staged privately, handed to the single
        Application Service entry point, and removed again before the response is built.

        Reading the body is asynchronous, but the import itself is a blocking call that scans and
        publishes to the graph. It runs in a worker thread so a slow import cannot stall the event
        loop and every other request with it.
        """
        validate_import_fields(name, workspace_id)
        application = service_of(request)
        try:
            async with staged_upload(file) as staged:
                result = await run_in_threadpool(
                    application.import_excel,
                    ExcelImportRequest(
                        file_path=str(staged.path),
                        datasource_name=name.strip() if name is not None else None,
                        workspace_id=workspace_id.strip(),
                    ),
                )
        except ExcelIngestionError as error:
            return excel_error_response(error)
        return JSONResponse(status_code=201, content=import_payload(result))

    @app.post("/api/datasources/{datasource_id}/scan")
    def scan_datasource(datasource_id: str):
        return service.scan_datasource(datasource_id)

    @app.get("/api/datasets")
    def list_datasets(workspace_id: str = Query("default"), datasource_id: str | None = None):
        return service.catalog.list_datasets(workspace_id, datasource_id)

    @app.get("/api/relations")
    def list_relations(
        workspace_id: str = Query("default"), datasource_id: str | None = None
    ) -> list[RelationInfo]:
        return service.catalog.list_relations(workspace_id, datasource_id)

    @app.get("/api/samples")
    def list_samples(workspace_id: str = Query("default"), datasource_id: str | None = None):
        return service.catalog.list_samples(workspace_id, datasource_id)

    @app.get("/api/schema-context")
    def schema_context(workspace_id: str = Query("default")):
        return build_schema_context(service.catalog, workspace_id)

    @app.post("/api/schema-summary")
    def schema_summary(workspace_id: str = Query("default")):
        context = build_schema_context(service.catalog, workspace_id)
        # The configured model port is owned by the application service; interfaces do not
        # build one, so every entry point shares the same provider and configuration.
        return {
            "summary": service.model.summarize_schema(context)
            if service.model
            else summarize_without_model(context)
        }

    @app.get("/api/mappings")
    def list_mappings(
        workspace_id: str = Query("default"), entity: str | None = None
    ) -> list[MappingInfo]:
        return service.catalog.list_mappings(workspace_id, entity)

    @app.post("/api/ask")
    def ask_data(request: AskRequest):
        return service.ask(request)

    @app.post("/api/ask/stream")
    async def stream_ask(request: AskRequest, http_request: Request) -> StreamingResponse:
        """Stream one Ask as Server-Sent Events.

        The request contract is the same ``AskRequest`` the synchronous endpoint takes, and the
        events carried here are the same ``AskEvent`` objects ``QanerisService.ask_stream()``
        produces: this route transports them and does not translate them.

        A clarification and a product failure are complete, successful streams - the status stays
        200 and ``event_type`` carries the verdict, so a client branches on the last event rather
        than on an HTTP status. Only a malformed body is refused before the stream starts, and that
        refusal is FastAPI's own request validation.

        This coroutine performs no blocking work: the pipeline is driven in worker threads inside
        the SSE generator. The response headers come from ``SSE_HEADERS`` so the media type is
        spelled exactly ``text/event-stream``.
        """
        return StreamingResponse(
            stream_ask_events(service_of(http_request), request, http_request),
            headers=SSE_HEADERS,
        )

    @app.get("/api/governance/suggestions")
    def governance_suggestions(workspace_id: str = Query("default")):
        return service.list_suggestions(workspace_id)

    @app.get("/api/governance/published-structure")
    def published_structure(workspace_id: str = Query("default"), datasource_id: str | None = None):
        """The graph node ids a JoinMapping must reference, so they can be selected in the UI.

        Separate from ``/api/schema-context`` on purpose: that one is catalog-backed and therefore
        has no node ids, while this one reads the published graph. Keeping them apart means a
        renderer never has to guess which identifiers are confirmable.
        """
        return service.describe_published_structure(workspace_id, datasource_id)

    # Credential and certificate upload. They resolve the same ``app.state.service`` every other
    # route does and call only its public managed-secret methods.
    register_credential_routes(app)
    register_datasource_routes(app)
    register_conversation_routes(app, service, path)

    @app.get("/", include_in_schema=False)
    def index():
        dist = web_dir / "frontend" / "dist"
        return FileResponse(dist / "index.html" if (dist / "index.html").exists() else web_dir / "index.html")

    dist_assets = web_dir / "frontend" / "dist" / "assets"
    if dist_assets.exists():
        app.mount("/assets", StaticFiles(directory=dist_assets), name="react-assets")

    return app


app = create_app()


def main() -> None:
    uvicorn.run("qaneris.interfaces.api.app:app", host="127.0.0.1", port=8000, reload=False)
