from __future__ import annotations

import os
from pathlib import Path


def project_root() -> Path:
    configured = os.getenv("QANERIS_PROJECT_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def artifact_root() -> Path:
    """Return the single external root for scan and agent-generated files."""
    configured = os.getenv("QANERIS_ARTIFACT_ROOT")
    if configured:
        root = Path(configured).expanduser().resolve()
    else:
        root = (project_root().parent / "QanerisArtifacts").resolve()
    repository = project_root()
    if root == repository or repository in root.parents:
        raise ValueError(
            "QANERIS_ARTIFACT_ROOT must be outside the Qaneris project directory"
        )
    return root


def artifact_directory(capability: str) -> Path:
    if not capability or capability in {".", ".."} or "/" in capability or "\\" in capability:
        raise ValueError("artifact capability must be one safe path segment")
    return artifact_root() / capability
