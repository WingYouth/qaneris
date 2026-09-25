from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from smartdata.common.artifacts import artifact_root
from smartdata.graph import Neo4jConfig
from smartdata.llm import resolve_model_profile


def _writable_destination(path: Path) -> bool:
    if path.exists():
        return os.access(path, os.W_OK)
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate.is_dir() and os.access(candidate, os.W_OK)


def inspect_environment() -> dict[str, dict[str, Any]]:
    """Return a credential-free Phase 1 environment diagnosis."""

    checks: dict[str, dict[str, Any]] = {}
    try:
        root = artifact_root()
        checks["artifact_root"] = {
            "status": "available" if _writable_destination(root) else "unavailable"
        }
    except Exception:  # noqa: BLE001 - diagnosis never exposes exception contents
        checks["artifact_root"] = {"status": "unavailable"}

    try:
        profile = resolve_model_profile()
        checks["model_configuration"] = {
            "status": "configured" if profile is not None else "missing"
        }
    except Exception:  # noqa: BLE001 - invalid model configuration is simply missing
        checks["model_configuration"] = {"status": "missing"}

    config: Neo4jConfig | None = None
    try:
        config = Neo4jConfig.from_environment()
        checks["neo4j_configuration"] = {"status": "configured"}
    except Exception:  # noqa: BLE001 - invalid graph configuration is simply missing
        checks["neo4j_configuration"] = {"status": "missing"}

    connectivity = "unavailable"
    if config is not None:
        driver = None
        try:
            from neo4j import GraphDatabase

            driver = GraphDatabase.driver(
                config.uri,
                auth=(config.username, config.password),
                database=config.database,
            )
            driver.verify_connectivity()
            connectivity = "available"
        except Exception:  # noqa: BLE001 - all connection failures map to unavailable
            connectivity = "unavailable"
        finally:
            if driver is not None:
                driver.close()
    checks["neo4j_connectivity"] = {"status": connectivity}

    catalog = Path(os.getenv("SMARTDATA_CATALOG", "smartdata.db")).expanduser()
    checks["catalog_configuration"] = {
        "status": "available" if _writable_destination(catalog.resolve()) else "unavailable"
    }
    return checks


def doctor_passed(checks: dict[str, dict[str, Any]]) -> bool:
    return all(item["status"] in {"available", "configured"} for item in checks.values())
