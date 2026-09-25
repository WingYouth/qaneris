"""Legacy five-database acceptance adapter for ``MultiDatabaseScanRunner``."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

from qaneris.cli.source import print_scan_report
from qaneris.common.environment import load_runtime_environment
from qaneris.contracts import (
    AuthenticationConfig,
    AuthenticationMethod,
    ConnectionEndpoint,
    ConnectionProfile,
    SecretProviderKind,
    SecretReference,
    SecureDatasourceCreate,
)
from qaneris.scanning import MultiDatabaseScanRunner


def _url_request(
    name: str,
    kind: str,
    driver: str,
    url: str,
    *,
    database: str | None = None,
) -> SecureDatasourceCreate:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    netloc = f"{host}:{parsed.port}" if parsed.port else host
    safe_url = urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
    authentication = AuthenticationConfig(username=unquote(parsed.username or "") or None)
    if parsed.password is not None:
        secret_name = f"QANERIS_ACCEPTANCE_{driver.upper()}_PASSWORD"
        os.environ[secret_name] = unquote(parsed.password)
        authentication = AuthenticationConfig(
            method=AuthenticationMethod.PASSWORD,
            username=unquote(parsed.username or "") or None,
            password=SecretReference(
                provider=SecretProviderKind.ENVIRONMENT,
                identifier=secret_name,
            ),
        )
    return SecureDatasourceCreate(
        name=name,
        kind=kind,
        connection_profile=ConnectionProfile(
            driver=driver,
            endpoint=ConnectionEndpoint(url=safe_url, database=database),
            authentication=authentication,
        ),
    )


def requests_from_environment() -> list[SecureDatasourceCreate]:
    dataset_root = Path(os.environ["NL_QUERY_DATASET_DIR"]).expanduser().resolve()
    return [
        SecureDatasourceCreate(
            name="acceptance-sqlite",
            kind="relational",
            connection_profile=ConnectionProfile(
                driver="sqlite",
                endpoint=ConnectionEndpoint(
                    path=str(dataset_root / "data" / "sqlite" / "retail.db")
                ),
            ),
        ),
        _url_request(
            "acceptance-postgresql", "relational", "postgresql", os.environ["SD_POSTGRES_URL"]
        ),
        _url_request(
            "acceptance-mysql", "relational", "mysql", os.environ["SD_MYSQL_URL"]
        ),
        _url_request(
            "acceptance-mongodb",
            "document",
            "mongodb",
            os.environ["SD_MONGO_URL"],
            database="retail",
        ),
        _url_request("acceptance-redis", "key_value", "redis", os.environ["SD_REDIS_URL"]),
    ]


def main() -> int:
    load_runtime_environment()
    report = MultiDatabaseScanRunner().scan(
        requests_from_environment(), config_source="legacy:scan_5db"
    )
    print_scan_report(report, json_output=False)
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
