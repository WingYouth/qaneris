import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from examples.create_demo_database import create_demo_database
from qaneris.interfaces.api.app import create_app


def test_api_reports_missing_and_unready_datasources(tmp_path: Path) -> None:
    source_path = tmp_path / "source.db"
    with sqlite3.connect(source_path) as connection:
        connection.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY)")
    app = create_app(str(tmp_path / "catalog.db"))

    with TestClient(app) as client:
        missing = client.post("/api/ask", json={"question": "orders 有多少条"})
        unknown = client.post(
            "/api/ask",
            json={"question": "orders 有多少条", "datasource_id": "ds_missing"},
        )
        created = client.post(
            "/api/datasources",
            json={
                "name": "source",
                "kind": "relational",
                "connection": {"driver": "sqlite", "path": str(source_path)},
            },
        )
        unready = client.post(
            "/api/ask",
            json={
                "question": "orders 有多少条",
                "datasource_id": created.json()["id"],
            },
        )

    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "datasource_unavailable"
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "datasource_not_found"
    assert unready.status_code == 400
    assert unready.json()["error"]["code"] == "datasource_not_ready"


def test_api_reports_validation_model_configuration_and_safety_errors(
    tmp_path: Path, monkeypatch
) -> None:
    # The natural-language path explicitly requires an intent model; this test exercises that
    # precondition without depending on a live provider. SQL safety stays on the legacy path.
    for key in ("QANERIS_MODEL_PROFILE", "QANERIS_MODEL_BASE_URL",
                "QANERIS_MODEL_API_KEY", "QANERIS_MODEL_NAME"):
        monkeypatch.delenv(key, raising=False)
    source_path = create_demo_database(tmp_path / "sales.db")
    app = create_app(str(tmp_path / "catalog.db"))

    with TestClient(app) as client:
        invalid = client.post("/api/ask", json={"question": ""})
        created = client.post(
            "/api/datasources",
            json={
                "name": "sales",
                "kind": "relational",
                "connection": {"driver": "sqlite", "path": str(source_path)},
            },
        )
        datasource_id = created.json()["id"]
        client.post(f"/api/datasources/{datasource_id}/scan").raise_for_status()
        ambiguous = client.post(
            "/api/ask",
            json={"question": "给我看一下数据", "datasource_id": datasource_id},
        )
        unsafe = client.post(
            "/api/ask",
            json={
                "question": "删除订单",
                "datasource_id": datasource_id,
                "sql": "DELETE FROM orders",
            },
        )
        average = client.post(
            "/api/ask",
            json={
                "question": "orders 中 total_amount 的平均值",
                "datasource_id": datasource_id,
                "sql": "SELECT ROUND(AVG(total_amount), 2) AS average_total_amount FROM orders",
            },
        )

    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "validation_error"
    assert ambiguous.status_code == 400
    assert ambiguous.json()["error"]["code"] == "intent_parsing_failed"
    assert unsafe.status_code == 400
    assert unsafe.json()["error"]["code"] == "unsafe_query"
    assert average.json()["result"]["rows"] == [{"average_total_amount": 5044.61}]
    assert average.json()["analysis"]["numeric_summary"]["average_total_amount"] == {
        "min": 5044.61,
        "max": 5044.61,
        "average": 5044.61,
        "sum": 5044.61,
    }
