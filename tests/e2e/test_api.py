import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from examples.create_demo_database import create_demo_database
from qaneris.contracts import DatasourceCreate, MappingInfo, RelationInfo
from qaneris.interfaces.api.app import create_app


def test_relation_and_mapping_endpoints_filter_catalog_data(tmp_path: Path) -> None:
    app = create_app(str(tmp_path / "catalog.db"))
    catalog = app.state.service.catalog
    datasource = catalog.create_datasource(
        DatasourceCreate(
            name="sales",
            kind="relational",
            connection={"driver": "sqlite", "path": "sales.db"},
            workspace_id="workspace_a",
        )
    )
    relation = RelationInfo(
        datasource_id=datasource.id,
        from_dataset="orders",
        from_field="customer_id",
        to_dataset="customers",
        to_field="id",
        relation_type="foreign_key",
    )
    catalog.replace_relations(datasource.id, [relation])
    mapping = catalog.create_mapping(
        MappingInfo(
            workspace_id="workspace_a",
            entity="customer",
            canonical_field="customer_id",
            datasource_id=datasource.id,
            dataset="customers",
            field="id",
        )
    )

    with TestClient(app) as client:
        relations_response = client.get(
            "/api/relations",
            params={"workspace_id": "workspace_a", "datasource_id": datasource.id},
        )
        mappings_response = client.get(
            "/api/mappings",
            params={"workspace_id": "workspace_a", "entity": "customer"},
        )
        other_workspace_response = client.get(
            "/api/mappings", params={"workspace_id": "workspace_b"}
        )

    assert relations_response.status_code == 200
    assert relations_response.json() == [relation.model_dump(mode="json")]
    assert mappings_response.status_code == 200
    assert mappings_response.json() == [mapping.model_dump(mode="json")]
    assert other_workspace_response.json() == []


def test_existing_sqlite_explicit_sql_api_workflow_remains_available(tmp_path: Path) -> None:
    source_path = tmp_path / "sales.db"
    with sqlite3.connect(source_path) as connection:
        connection.executescript(
            """
            CREATE TABLE orders (id INTEGER PRIMARY KEY, amount REAL NOT NULL);
            INSERT INTO orders VALUES (1, 10.5), (2, 20.0);
            """
        )
    app = create_app(str(tmp_path / "catalog.db"))

    with TestClient(app) as client:
        create_response = client.post(
            "/api/datasources",
            json={
                "name": "sales",
                "kind": "relational",
                "connection": {"driver": "sqlite", "path": str(source_path)},
            },
        )
        datasource_id = create_response.json()["id"]
        scan_response = client.post(f"/api/datasources/{datasource_id}/scan")
        ask_response = client.post(
            "/api/ask",
            json={"question": "orders amount 总额", "datasource_id": datasource_id,
                  "sql": "SELECT SUM(amount) AS total_amount FROM orders"},
        )

    assert create_response.status_code == 201
    assert scan_response.status_code == 200
    assert ask_response.status_code == 200
    assert ask_response.json()["result"]["rows"] == [{"total_amount": 30.5}]
    assert ask_response.json()["plan"][0]["query_language"] == "sql"


def test_api_lists_all_multi_database_adapters(tmp_path: Path) -> None:
    with TestClient(create_app(str(tmp_path / "catalog.db"))) as client:
        response = client.get("/api/adapters")

    assert response.status_code == 200
    adapters = {(item["kind"], item["driver"]) for item in response.json()}
    assert len(adapters) >= 21
    assert ("relational", "postgresql") in adapters
    assert ("vector", "qdrant") in adapters


def test_limited_join_queries_run_through_explicit_sql_api(tmp_path: Path) -> None:
    source_path = create_demo_database(tmp_path / "sales.db")
    app = create_app(str(tmp_path / "catalog.db"))
    contract_path = Path(__file__).parents[2] / "examples" / "questions_2026_07.json"
    contracts = json.loads(contract_path.read_text(encoding="utf-8"))["cases"]
    cases = [case for case in contracts if case["id"] in {
        "category-sales", "region-sales", "top-products-by-quantity"
    }]

    with TestClient(app) as client:
        create_response = client.post(
            "/api/datasources",
            json={
                "name": "sales",
                "kind": "relational",
                "connection": {"driver": "sqlite", "path": str(source_path)},
            },
        )
        datasource_id = create_response.json()["id"]
        client.post(f"/api/datasources/{datasource_id}/scan").raise_for_status()
        responses = [
            client.post(
                "/api/ask",
                json={"question": case["question"], "datasource_id": datasource_id,
                      "sql": case["verification_sql"]},
            )
            for case in cases
        ]

    for response, case in zip(responses, cases, strict=True):
        assert response.status_code == 200
        payload = response.json()
        assert len(payload["result"]["rows"]) == len(case["expected_rows"])
        assert all(payload["result"]["rows"][index][field] == value
                   for index, expected in enumerate(case["expected_rows"])
                   for field, value in expected.items())
        assert " JOIN " in payload["plan"][0]["query"]


def test_standard_verification_sql_contract_runs_through_api(tmp_path: Path) -> None:
    source_path = create_demo_database(tmp_path / "sales.db")
    contract_path = Path(__file__).parents[2] / "examples" / "questions_2026_07.json"
    cases = json.loads(contract_path.read_text(encoding="utf-8"))["cases"]
    app = create_app(str(tmp_path / "catalog.db"))

    with TestClient(app) as client:
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
        responses = [
            client.post(
                "/api/ask",
                json={"question": case["question"], "datasource_id": datasource_id,
                      "sql": case["verification_sql"]},
            )
            for case in cases
        ]

    for response, case in zip(responses, cases, strict=True):
        assert response.status_code == 200, case["id"]
        rows = response.json()["result"]["rows"]
        expected = case["expected_rows"]
        projected = [
            {key: row[key] for key in expected_row}
            for row, expected_row in zip(rows, expected, strict=True)
        ]
        assert projected == expected, case["id"]
