from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from fastapi.testclient import TestClient

from smartdata.connections.tls_matrix import TLS_DRIVER_MATRIX
from smartdata.interfaces.api.app import create_app


def client(tmp_path):
    app = create_app(str(tmp_path / "catalog.db"))
    calls = []
    app.state.service = SimpleNamespace(
        test_secure_datasource=lambda body: calls.append(("test", body)) or {"ok": True, "driver": "sqlite", "tls_enabled": False},
        create_secure_datasource=lambda body: calls.append(("create", body)) or {"id": "ds_new", "status": "created"},
        inspect_datasource=lambda identity: {"id": identity, "driver": "sqlite", "status": "created", "workspace_id": "default", "scan_version": None, "last_scan_status": None, "dataset_count": 0},
        update_secure_datasource=lambda identity, body: calls.append(("update", identity, body)) or {"id": identity, "status": "created"},
        delete_datasource=lambda identity: calls.append(("delete", identity)),
        scan_datasource=lambda identity: [],
        list_tls_capabilities=lambda: [{"driver": s.driver, "custom_ca": s.custom_ca, "mtls": s.mtls, "server_name_override": s.server_name_override, "tls13_control": s.tls13_control} for s in TLS_DRIVER_MATRIX.values()],
        list_supported_adapters=list,
    )
    return TestClient(app), calls


def test_secure_datasource_lifecycle_http_contract(tmp_path):
    http, calls = client(tmp_path)
    profile = {"driver": "sqlite", "deployment_mode": "local", "endpoint": {"path": "/tmp/data.db"}, "authentication": {"method": "none"}, "tls": {"enabled": False}}
    assert http.post("/api/datasources/test", json={"kind": "relational", "connection_profile": profile}).json()["ok"]
    created = http.post("/api/datasources/secure", json={"name": "demo", "kind": "relational", "workspace_id": "default", "connection_profile": profile})
    assert created.status_code == 201 and created.json()["status"] == "created"
    inspected = http.get("/api/datasources/ds_new")
    assert inspected.status_code == 200
    assert "connection_profile" not in inspected.text
    assert http.put("/api/datasources/ds_new/secure", json={"connection_profile": profile}).json()["status"] == "created"
    assert "/api/datasources/{datasource_id}/scan" in http.get("/openapi.json").json()["paths"]
    assert http.delete("/api/datasources/ds_new").status_code == 204
    assert [entry[0] for entry in calls] == ["test", "create", "update", "delete"]


def test_tls_capabilities_are_public_matrix_projection(tmp_path):
    http, _ = client(tmp_path)
    response = http.get("/api/tls-capabilities")
    assert response.status_code == 200
    assert len(response.json()) == 16
    assert set(response.json()[0]) == {"driver", "custom_ca", "mtls", "server_name_override", "tls13_control"}


def test_sqlite_http_lifecycle_test_create_scan_inspect_delete(tmp_path):
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as connection:
        connection.execute("create table orders (id integer primary key, amount real)")
        connection.execute("insert into orders values (1, 10.5)")
    http = TestClient(create_app(str(tmp_path / "catalog.db")), raise_server_exceptions=False)
    profile = {"driver": "sqlite", "deployment_mode": "local", "endpoint": {"path": str(source)}, "authentication": {"method": "none"}, "tls": {"enabled": False}}
    candidate = {"kind": "relational", "connection_profile": profile}

    checked = http.post("/api/datasources/test", json=candidate)
    assert checked.status_code == 200 and checked.json() == {"ok": True, "driver": "sqlite", "tls_enabled": False}
    assert http.get("/api/datasources").json() == []
    created = http.post("/api/datasources/secure", json={"name": "temporary sqlite", "kind": "relational", "connection_profile": profile})
    assert created.status_code == 201 and created.json()["status"] == "created"
    identity = created.json()["id"]
    scanned = http.post(f"/api/datasources/{identity}/scan")
    assert scanned.status_code == 200
    inspected = http.get(f"/api/datasources/{identity}").json()
    assert inspected["status"] == "ready" and inspected["dataset_count"] == 1
    assert "connection_profile" not in inspected
    assert http.delete(f"/api/datasources/{identity}").status_code == 204
