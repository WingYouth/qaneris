from contextlib import contextmanager
from typing import Any

from smartdata.adapters.search.engine import SearchAdapter


def test_elasticsearch_defaults_to_all_indexes_for_connection_and_scan() -> None:
    class Indices:
        def get_mapping(self, *, index: str) -> dict[str, Any]:
            assert index == "*"
            return {}

    class Client:
        indices = Indices()

    adapter = SearchAdapter(
        "ds_elasticsearch",
        {"driver": "elasticsearch", "url": "http://127.0.0.1:9200"},
    )

    @contextmanager
    def fake_client():
        yield Client()

    adapter._client = fake_client  # type: ignore[method-assign]
    adapter.test_connection()
    assert adapter.scan_metadata() == []


def test_opensearch_uses_configured_business_index_pattern_for_connection_and_scan() -> None:
    class Indices:
        def get_mapping(self, *, index: str) -> dict[str, Any]:
            assert index == "retail_*"
            return {}

    class Client:
        indices = Indices()

    adapter = SearchAdapter(
        "ds_opensearch",
        {
            "driver": "opensearch",
            "url": "https://127.0.0.1:9201",
            "index_pattern": "retail_*",
        },
    )

    @contextmanager
    def fake_client():
        yield Client()

    adapter._client = fake_client  # type: ignore[method-assign]
    adapter.test_connection()
    assert adapter.scan_metadata() == []


def test_search_adapter_accepts_custom_index_patterns_without_business_coupling() -> None:
    for pattern in ("company-*", "logs-*"):
        class Indices:
            # ``expected`` binds the loop variable instead of closing over it (B023).
            def get_mapping(self, *, index: str, expected: str = pattern) -> dict[str, Any]:
                assert index == expected
                return {}

        class Client:
            indices = Indices()

        adapter = SearchAdapter(
            "ds_custom_search",
            {"driver": "elasticsearch", "url": "http://127.0.0.1:9200", "index_pattern": pattern},
        )

        @contextmanager
        def fake_client():
            yield Client()

        adapter._client = fake_client  # type: ignore[method-assign]
        adapter.test_connection()
        assert adapter.scan_metadata() == []
