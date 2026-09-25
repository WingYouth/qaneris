from __future__ import annotations

import pytest

from qaneris.common.environment import EnvironmentBootstrapError, load_runtime_environment


def test_explicit_env_file_wins_over_selector_and_cwd(tmp_path, monkeypatch) -> None:
    explicit = tmp_path / "explicit.env"
    selected = tmp_path / "selected.env"
    cwd = tmp_path / ".env"
    explicit.write_text("QANERIS_ENV_TEST=explicit\n", encoding="utf-8")
    selected.write_text("QANERIS_ENV_TEST=selected\n", encoding="utf-8")
    cwd.write_text("QANERIS_ENV_TEST=cwd\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("QANERIS_ENV_FILE", str(selected))
    monkeypatch.delenv("QANERIS_ENV_TEST", raising=False)

    loaded = load_runtime_environment(explicit)

    assert loaded == explicit.resolve()
    assert __import__("os").environ["QANERIS_ENV_TEST"] == "explicit"


def test_env_file_selector_wins_over_cwd(tmp_path, monkeypatch) -> None:
    selected = tmp_path / "selected.env"
    selected.write_text("QANERIS_ENV_TEST=selected\n", encoding="utf-8")
    (tmp_path / ".env").write_text("QANERIS_ENV_TEST=cwd\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("QANERIS_ENV_FILE", str(selected))
    monkeypatch.delenv("QANERIS_ENV_TEST", raising=False)

    assert load_runtime_environment() == selected.resolve()
    assert __import__("os").environ["QANERIS_ENV_TEST"] == "selected"


def test_cwd_dotenv_does_not_override_process_environment(tmp_path, monkeypatch) -> None:
    (tmp_path / ".env").write_text("QANERIS_ENV_TEST=dotenv\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("QANERIS_ENV_FILE", raising=False)
    monkeypatch.setenv("QANERIS_ENV_TEST", "process")

    assert load_runtime_environment() == tmp_path / ".env"
    assert __import__("os").environ["QANERIS_ENV_TEST"] == "process"


def test_selected_missing_file_is_configuration_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("QANERIS_ENV_FILE", str(tmp_path / "missing.env"))

    with pytest.raises(EnvironmentBootstrapError, match="does not exist"):
        load_runtime_environment()


def test_missing_cwd_dotenv_is_normal_and_parent_is_not_searched(tmp_path, monkeypatch) -> None:
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    (parent / ".env").write_text("QANERIS_ENV_TEST=parent\n", encoding="utf-8")
    monkeypatch.chdir(child)
    monkeypatch.delenv("QANERIS_ENV_FILE", raising=False)
    monkeypatch.delenv("QANERIS_ENV_TEST", raising=False)

    assert load_runtime_environment() is None
    assert "QANERIS_ENV_TEST" not in __import__("os").environ
