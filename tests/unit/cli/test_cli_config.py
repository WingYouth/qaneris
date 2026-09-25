import json

import pytest

from qaneris.cli.config import DatasourceConfigError, load_datasource_requests


def sqlite_request(path: str = "/tmp/source.db") -> dict:
    return {
        "name": "sales",
        "kind": "relational",
        "connection_profile": {
            "driver": "sqlite",
            "endpoint": {"path": path},
        },
    }


@pytest.mark.parametrize(
    "payload",
    [
        sqlite_request(),
        [sqlite_request()],
        {"datasources": [sqlite_request()]},
    ],
)
def test_load_datasource_requests_accepts_supported_shapes(tmp_path, payload) -> None:
    path = tmp_path / "datasources.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    requests = load_datasource_requests(path)

    assert len(requests) == 1
    assert requests[0].connection_profile.driver == "sqlite"


def test_invalid_config_error_does_not_echo_plaintext_password(tmp_path) -> None:
    payload = sqlite_request()
    payload["connection_profile"]["authentication"] = {
        "method": "password",
        "password": "do-not-print-me",
    }
    path = tmp_path / "datasources.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(DatasourceConfigError) as raised:
        load_datasource_requests(path)

    assert "do-not-print-me" not in str(raised.value)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.update({"password": "top-level-secret"}),
        lambda payload: payload["connection_profile"]["endpoint"].update(
            {"url": "postgresql://user:url-secret@localhost/db"}
        ),
    ],
)
def test_config_rejects_inline_secrets_even_in_ignored_or_url_fields(
    tmp_path, mutation
) -> None:
    payload = sqlite_request()
    mutation(payload)
    path = tmp_path / "datasources.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(DatasourceConfigError, match="inline secret") as raised:
        load_datasource_requests(path)

    assert "top-level-secret" not in str(raised.value)
    assert "url-secret" not in str(raised.value)
