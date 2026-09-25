"""Secure datasource HTTP lifecycle and public TLS capability projection."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from qaneris.contracts import SecureDatasourceCreate, SecureDatasourceTest, SecureDatasourceUpdate


def register_datasource_routes(app) -> None:
    router = APIRouter()

    @router.post("/api/datasources/test")
    def test_datasource(request: SecureDatasourceTest, http_request: Request):
        return http_request.app.state.service.test_secure_datasource(request)

    @router.post("/api/datasources/secure", status_code=201)
    def create_datasource(request: SecureDatasourceCreate, http_request: Request):
        return http_request.app.state.service.create_secure_datasource(request)

    @router.get("/api/datasources/{datasource_id}")
    def inspect_datasource(datasource_id: str, http_request: Request):
        return http_request.app.state.service.inspect_datasource(datasource_id)

    @router.post("/api/datasources/{datasource_id}/test")
    def test_saved_datasource(datasource_id: str, http_request: Request):
        return http_request.app.state.service.test_saved_datasource(datasource_id)

    @router.put("/api/datasources/{datasource_id}/secure")
    def update_datasource(datasource_id: str, request: SecureDatasourceUpdate, http_request: Request):
        return http_request.app.state.service.update_secure_datasource(datasource_id, request)

    @router.delete("/api/datasources/{datasource_id}", status_code=204)
    def delete_datasource(datasource_id: str, http_request: Request):
        http_request.app.state.service.delete_datasource(datasource_id)
        return Response(status_code=204)

    @router.get("/api/tls-capabilities")
    def tls_capabilities(http_request: Request):
        return http_request.app.state.service.list_tls_capabilities()

    app.include_router(router)
