from pathlib import Path

import pytest

from qaneris.common.artifacts import artifact_directory, artifact_root


def test_default_artifact_root_is_a_sibling_of_the_project(monkeypatch, tmp_path: Path) -> None:
    project = tmp_path / "Qaneris"
    monkeypatch.setenv("QANERIS_PROJECT_ROOT", str(project))
    monkeypatch.delenv("QANERIS_ARTIFACT_ROOT", raising=False)

    assert artifact_root() == tmp_path / "QanerisArtifacts"
    assert artifact_directory("scan-reports") == tmp_path / "QanerisArtifacts" / "scan-reports"


def test_artifact_root_inside_project_is_rejected(monkeypatch, tmp_path: Path) -> None:
    project = tmp_path / "Qaneris"
    monkeypatch.setenv("QANERIS_PROJECT_ROOT", str(project))
    monkeypatch.setenv("QANERIS_ARTIFACT_ROOT", str(project / "artifacts"))

    with pytest.raises(ValueError, match="must be outside"):
        artifact_root()
