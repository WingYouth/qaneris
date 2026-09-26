from contextlib import contextmanager

import pytest

from qaneris.adapters.vector.qdrant import QdrantAdapter


def test_qdrant_https_wrong_version_explains_server_setup() -> None:
    adapter = QdrantAdapter("ds", {"host": "localhost", "port": 6333, "tls": True})

    class Client:
        def get_collections(self) -> None:
            raise RuntimeError("[SSL: WRONG_VERSION_NUMBER] wrong version number")

    @contextmanager
    def fake_client():
        yield Client()

    adapter._client = fake_client  # type: ignore[method-assign]
    with pytest.raises(ConnectionError, match="目标端口未提供 HTTPS"):
        adapter.test_connection()
