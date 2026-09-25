from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError

from smartdata.contracts.connection import SecureDatasourceCreate


class DatasourceConfigError(ValueError):
    """Raised when a CLI datasource configuration is invalid."""


_SENSITIVE_KEYS = {
    "password",
    "passwd",
    "token",
    "api_key",
    "apikey",
    "secret",
    "private_key",
    "service_account",
    "client_private_key",
    "client_certificate",
    "ca_certificate",
}


def _contains_inline_secret(value: Any, key: str = "") -> bool:
    normalized = key.lower()
    if normalized in _SENSITIVE_KEYS and value not in (None, ""):
        return not isinstance(value, dict) or set(value) != {"provider", "identifier"}
    if isinstance(value, str) and normalized in {"url", "uri", "connection_string"}:
        try:
            return urlsplit(value).password is not None
        except ValueError:
            return True
    if isinstance(value, dict):
        return any(_contains_inline_secret(child, str(name)) for name, child in value.items())
    if isinstance(value, list):
        return any(_contains_inline_secret(child, key) for child in value)
    return False


def load_datasource_requests(path: Path) -> list[SecureDatasourceCreate]:
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise DatasourceConfigError(f"cannot read config file: {path}") from error
    except json.JSONDecodeError as error:
        raise DatasourceConfigError(
            f"invalid JSON at line {error.lineno}, column {error.colno}"
        ) from error

    if isinstance(payload, dict) and "datasources" in payload:
        payload = payload["datasources"]
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list) or not payload:
        raise DatasourceConfigError(
            "config must be one datasource, a non-empty list, or an object with datasources"
        )

    requests: list[SecureDatasourceCreate] = []
    for index, item in enumerate(payload):
        if _contains_inline_secret(item):
            raise DatasourceConfigError(
                f"datasource[{index}] contains an inline secret; use SecretReference"
            )
        try:
            requests.append(SecureDatasourceCreate.model_validate(item))
        except ValidationError as error:
            details = [
                {
                    "location": ".".join(str(part) for part in issue["loc"]),
                    "type": issue["type"],
                    "message": issue["msg"],
                }
                for issue in error.errors(include_url=False, include_input=False)
            ]
            raise DatasourceConfigError(
                f"datasource[{index}] is invalid: {json.dumps(details, ensure_ascii=False)}"
            ) from error
    return requests


def load_single_datasource_request(path: Path) -> SecureDatasourceCreate:
    """Load a config that must describe exactly one datasource.

    The single-datasource secure commands (``test-one`` / ``create`` / ``update``) act on one
    connection, so a file holding several is a usage error rather than something to pick from
    silently. The parsing, the inline-secret refusal and the contract validation are all reused
    unchanged, so a single-datasource command accepts exactly what a batch command accepts.
    """
    requests = load_datasource_requests(path)
    if len(requests) != 1:
        raise DatasourceConfigError(
            f"config must contain exactly one datasource, found {len(requests)}"
        )
    return requests[0]
