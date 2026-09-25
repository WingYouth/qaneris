import pytest

from qaneris.querying.validation.read_only import UnsafeQueryError, validate_read_only_query


@pytest.mark.parametrize(
    "query",
    ["SELECT * FROM orders", "WITH recent AS (SELECT 1 AS id) SELECT * FROM recent;"],
)
def test_accepts_read_only_queries(query: str) -> None:
    validate_read_only_query(query)


@pytest.mark.parametrize(
    "query",
    [
        "DELETE FROM orders",
        "SELECT * FROM orders; DROP TABLE orders",
        "PRAGMA table_info(orders)",
        "WITH target AS (SELECT 1) UPDATE orders SET amount=0",
    ],
)
def test_rejects_unsafe_queries(query: str) -> None:
    with pytest.raises(UnsafeQueryError):
        validate_read_only_query(query)
