import sqlite3
from pathlib import Path

from examples.verify_adapter import verify_adapter


def test_adapter_verifier_checks_connection_and_three_row_limit(tmp_path: Path) -> None:
    database = tmp_path / "source.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE events(id INTEGER PRIMARY KEY, value TEXT)")
        connection.executemany(
            "INSERT INTO events(id, value) VALUES(?, ?)",
            [(index, f"value-{index}") for index in range(1, 6)],
        )

    result = verify_adapter("relational", "sqlite", {"path": str(database)})

    assert result == {
        "kind": "relational",
        "driver": "sqlite",
        "connection": "ok",
        "dataset_count": 1,
        "relation_count": 0,
        "sampled_dataset_count": 1,
        "sample_row_count": 3,
    }
