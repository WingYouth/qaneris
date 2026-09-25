from __future__ import annotations

import os
import re
import tempfile
import unicodedata
from pathlib import Path

from smartdata.common.artifacts import artifact_directory, project_root
from smartdata.contracts.profile import ScanSnapshot
from smartdata.profiling.documents.markdown import MarkdownProfileRenderer


def default_profile_document_root(catalog_path: str) -> Path:
    configured = os.getenv("SMARTDATA_PROFILE_DIR")
    if configured:
        root = Path(configured).expanduser().resolve()
        # Keep the legacy override, but subject it to the same repository boundary.
        repository = project_root()
        if root == repository or repository in root.parents:
            raise ValueError("SMARTDATA_PROFILE_DIR must be outside the SmartData project directory")
        return root
    if catalog_path != ":memory:":
        catalog_root = Path(catalog_path).expanduser().resolve().parent
        repository = project_root()
        # Co-locate with externally configured runtime state when that state is already
        # outside the source tree. This is convenient for isolated deployments and tests.
        if catalog_root != repository and repository not in catalog_root.parents:
            return catalog_root / "profiles"
    return artifact_directory("scan-reports")


class FileSystemProfileDocumentWriter:
    def __init__(self, root: str | Path, renderer: MarkdownProfileRenderer | None = None):
        self.root = Path(root).expanduser().resolve()
        self.renderer = renderer or MarkdownProfileRenderer()

    def write(self, snapshot: ScanSnapshot, workspace_id: str) -> Path:
        if snapshot.profile is None:
            raise ValueError("profile document requires a scan profile")
        documents = self.renderer.render(snapshot, workspace_id)
        self.root.mkdir(parents=True, exist_ok=True)
        directory_name = safe_datasource_directory_name(snapshot.profile.name)
        target = self.root / directory_name
        if target.resolve().parent != self.root:
            raise ValueError("datasource profile path escapes the configured root")
        target.mkdir(parents=True, exist_ok=True)
        for relative_path, content in documents.items():
            relative = Path(relative_path)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"unsafe profile document path: {relative_path}")
            path = (target / relative).resolve()
            if target not in path.parents:
                raise ValueError(f"profile document path escapes datasource directory: {relative_path}")
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix=f".{path.name}-",
                dir=path.parent,
                delete=False,
            ) as temporary:
                temporary.write(content)
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, path)
        return target


def safe_datasource_directory_name(value: str) -> str:
    """Return a readable datasource directory name that cannot escape its root."""
    name = unicodedata.normalize("NFKC", value).strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]+', "-", name)
    name = re.sub(r"\s+", "-", name)
    while ".." in name:
        name = name.replace("..", "-")
    name = re.sub(r"-+", "-", name).strip(" .-")
    if not name or name in {".", ".."}:
        raise ValueError("datasource name does not contain a safe directory name")
    return name
