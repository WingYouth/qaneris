from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import quote

_SECRET_MARKERS = ("PASSWORD", "SECRET", "TOKEN", "API_KEY", "CREDENTIAL", "PRIVATE_KEY")
_CREDENTIAL_URL = re.compile(
    r"(?P<prefix>[A-Za-z][A-Za-z0-9+.-]*://[^\s:/@]+:)(?P<secret>[^\s/@]+)(?P<suffix>@)"
)


class SecretRedactor:
    """Redact resolved secrets and passwords embedded in connection URLs."""

    def __init__(self, values: Iterable[str] = ()):
        self._values = {value for value in values if value and len(value) >= 4}

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
        extra_values: Iterable[str] = (),
    ) -> SecretRedactor:
        source = os.environ if environment is None else environment
        values = [
            value
            for key, value in source.items()
            if any(marker in key.upper() for marker in _SECRET_MARKERS)
        ]
        return cls((*values, *extra_values))

    def text(self, value: object) -> str:
        safe = str(value)
        for secret in sorted(self._values, key=len, reverse=True):
            safe = safe.replace(secret, "<redacted>")
            safe = safe.replace(quote(secret, safe=""), "<redacted>")
        return _CREDENTIAL_URL.sub(r"\g<prefix><redacted>\g<suffix>", safe)

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {str(key): self.value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.value(item) for item in value]
        if isinstance(value, tuple):
            return [self.value(item) for item in value]
        return value


def safe_error(error: Exception, *, secrets: Iterable[str] = ()) -> str:
    redactor = SecretRedactor.from_environment(extra_values=secrets)
    return redactor.text(f"{type(error).__name__}: {error}")


def sensitive_values(value: Any, key: str = "") -> list[str]:
    """Collect only credential values from a materialized adapter connection."""

    if isinstance(value, dict):
        collected: list[str] = []
        for name, item in value.items():
            if any(marker in str(name).upper() for marker in _SECRET_MARKERS):
                if isinstance(item, str):
                    collected.append(item)
                continue
            collected.extend(sensitive_values(item, str(name)))
        return collected
    if isinstance(value, list):
        return [secret for item in value for secret in sensitive_values(item, key)]
    return []
