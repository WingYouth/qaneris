"""CLI contract tests for the secure ``source`` lifecycle commands (RS-CLI-02).

These lock the interface contract only: the argument wiring, the exact number of Application
Service calls per command, the candidate-first update rule, the "no implicit scan" rule, stdout
purity for JSON reports, the stable product error and exit codes, and the absence of any connection
material in the output.

The Application Service's own behaviour - connection testing, TLS materialization, catalog
ordering - is covered by the Core suites and is deliberately not retested here.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock, call

import pytest

from smartdata.application.service import SmartDataService
from smartdata.cli import main as cli
from smartdata.cli import source as source_cli
from smartdata.cli.config import DatasourceConfigError
from smartdata.common.errors import DatasourceNotFoundError
from smartdata.contracts import DatasetInfo, Datasource, DatasourceDetail
from smartdata.contracts.connection import SecureDatasourceTestResult

DATASOURCE_ID = "ds_ff606cfffe96"

# Material that must never reach a CLI report: the resolved connection and every secret value.
SECRET_PASSWORD = "s3cret-cli-marker"
SECRET_URL = f"postgresql://ro:{SECRET_PASSWORD}@10.0.0.5/prod"


def sqlite_config(**profile: Any) -> dict[str, Any]:
    """One secure datasource config in exactly the format ``source create`` accepts."""
    connection: dict[str, Any] = {"driver": "sqlite", "endpoint": {"path": "/tmp/source.db"}}
    connection.update(profile)
    return {"name": "sales", "kind": "relational", "connection_profile": connection}


def write_config(tmp_path: Path, payload: Any, name: str = "datasources.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def datasource(**changes: Any) -> Datasource:
    values: dict[str, Any] = {
        "id": DATASOURCE_ID,
        "name": "sales",
        "kind": "relational",
        "workspace_id": "default",
        "driver": "sqlite",
        "status": "created",
    }
    values.update(changes)
    return Datasource(**values)


def detail(**changes: Any) -> DatasourceDetail:
    values: dict[str, Any] = {
        "id": DATASOURCE_ID,
        "name": "sales",
        "kind": "relational",
        "workspace_id": "default",
        "driver": "sqlite",
        "status": "created",
        "scan_version": 0,
        "last_scan_status": None,
        "dataset_count": 0,
    }
    values.update(changes)
    return DatasourceDetail(**values)


def installed(monkeypatch: pytest.MonkeyPatch, service: Any) -> Mock:
    constructor = Mock(return_value=service)
    monkeypatch.setattr(source_cli, "SmartDataService", constructor)
    return constructor


def serving(monkeypatch: pytest.MonkeyPatch, **methods: Any) -> Any:
    service = Mock(spec=SmartDataService, **methods)
    installed(monkeypatch, service)
    return service


def run(capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, str, str]:
    try:
        code = cli.main(argv)
    except SystemExit as error:  # argparse usage errors
        code = error.code if isinstance(error.code, int) else 1
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# --------------------------------------------------------------------------------------------
# source test-one
# --------------------------------------------------------------------------------------------


def test_test_one_calls_only_the_candidate_test(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A working connection is reported, and nothing is written: the test is not a create."""
    testing = Mock(return_value=SecureDatasourceTestResult(driver="sqlite", tls_enabled=False))
    service = serving(monkeypatch, test_secure_datasource=testing)
    path = write_config(tmp_path, sqlite_config())

    code, stdout, stderr = run(capsys, ["source", "test-one", "--config", str(path)])

    assert code == 0
    assert stderr == ""
    assert testing.call_count == 1
    request = testing.call_args.args[0]
    assert request.kind == "relational"
    assert request.connection_profile.driver == "sqlite"
    assert "[PASS] Datasource connection test passed" in stdout
    assert "Driver: sqlite" in stdout
    # No catalog write of any kind: test-one is the ``Test`` half of Test -> Save -> Scan.
    assert service.create_secure_datasource.call_count == 0
    assert service.scan_datasource.call_count == 0


def test_test_one_json_carries_only_three_public_facts(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    serving(
        monkeypatch,
        test_secure_datasource=Mock(
            return_value=SecureDatasourceTestResult(driver="postgresql", tls_enabled=True)
        ),
    )
    path = write_config(tmp_path, sqlite_config())

    code, stdout, _ = run(
        capsys, ["source", "test-one", "--config", str(path), "--json"]
    )

    assert code == 0
    assert json.loads(stdout) == {
        "status": "completed",
        "driver": "postgresql",
        "tls_enabled": True,
    }


def test_test_one_failure_is_exit_one_with_a_stable_code(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from smartdata.common.errors import SmartDataError

    class ConnectionRefused(SmartDataError):
        code = "connection_test_failed"

    serving(monkeypatch, test_secure_datasource=Mock(side_effect=ConnectionRefused("refused")))
    path = write_config(tmp_path, sqlite_config())

    code, stdout, _ = run(capsys, ["source", "test-one", "--config", str(path), "--json"])

    assert code == 1
    document = json.loads(stdout)
    assert document["status"] == "operation_failed"
    assert document["error"]["code"] == "connection_test_failed"


# --------------------------------------------------------------------------------------------
# source create
# --------------------------------------------------------------------------------------------


def test_create_calls_the_service_once_and_does_not_scan(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Create is not a scan: the datasource stays at ``created`` until ``scan-one`` is run."""
    creating = Mock(return_value=datasource(status="created"))
    service = serving(monkeypatch, create_secure_datasource=creating)
    path = write_config(tmp_path, sqlite_config())

    code, stdout, stderr = run(capsys, ["source", "create", "--config", str(path)])

    assert code == 0
    assert stderr == ""
    assert creating.call_count == 1
    assert creating.call_args.args[0].name == "sales"
    assert "Status: created" in stdout
    assert f"Next: smartdata source scan-one {DATASOURCE_ID}" in stdout
    assert service.scan_datasource.call_count == 0
    assert service.test_secure_datasource.call_count == 0


def test_create_json_reports_identity_and_status_only(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    serving(monkeypatch, create_secure_datasource=Mock(return_value=datasource()))
    path = write_config(tmp_path, sqlite_config())

    code, stdout, _ = run(capsys, ["source", "create", "--config", str(path), "--json"])

    assert code == 0
    document = json.loads(stdout)
    assert document["status"] == "completed"
    assert document["datasource"] == {
        "id": DATASOURCE_ID,
        "name": "sales",
        "kind": "relational",
        "driver": "sqlite",
        "status": "created",
        "workspace_id": "default",
    }


def test_create_output_never_contains_connection_material(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A profile that names a managed secret and a host must not leak the host or the reference."""
    serving(monkeypatch, create_secure_datasource=Mock(return_value=datasource()))
    payload = sqlite_config(
        driver="postgresql",
        endpoint={"hosts": [{"host": "10.0.0.5", "port": 5432}], "database": "prod"},
        authentication={
            "method": "password",
            "username": "ro",
            "password": {"provider": "managed", "identifier": "sec_deadbeef"},
        },
    )
    path = write_config(tmp_path, payload)

    code, stdout, _ = run(capsys, ["source", "create", "--config", str(path)])

    assert code == 0
    for leaked in ("10.0.0.5", "sec_deadbeef", "ro"):
        assert leaked not in stdout


# --------------------------------------------------------------------------------------------
# source update
# --------------------------------------------------------------------------------------------


def test_update_proves_the_identity_then_calls_update_once(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    inspecting = Mock(return_value=detail())
    updating = Mock(return_value=datasource(status="created"))
    service = serving(
        monkeypatch, inspect_datasource=inspecting, update_secure_datasource=updating
    )
    path = write_config(tmp_path, sqlite_config())

    code, stdout, stderr = run(
        capsys, ["source", "update", DATASOURCE_ID, "--config", str(path)]
    )

    assert code == 0
    assert stderr == ""
    assert inspecting.call_count == 1
    assert updating.call_count == 1
    assert updating.call_args.args[0] == DATASOURCE_ID
    request = updating.call_args.args[1]
    assert request.connection_profile.driver == "sqlite"
    assert "Status: created" in stdout
    assert "Previous scan invalidated: yes" in stdout
    assert service.scan_datasource.call_count == 0


def test_update_json_marks_the_scan_as_required(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    serving(
        monkeypatch,
        inspect_datasource=Mock(return_value=detail()),
        update_secure_datasource=Mock(return_value=datasource(status="created")),
    )
    path = write_config(tmp_path, sqlite_config())

    code, stdout, _ = run(
        capsys, ["source", "update", DATASOURCE_ID, "--config", str(path), "--json"]
    )

    assert code == 0
    document = json.loads(stdout)
    assert document["scan_required"] is True
    assert document["datasource"]["status"] == "created"


def test_update_refuses_a_config_that_names_a_different_datasource(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Identity is immutable, so a config describing another datasource is a usage error."""
    updating = Mock()
    service = serving(
        monkeypatch,
        inspect_datasource=Mock(return_value=detail(name="warehouse")),
        update_secure_datasource=updating,
    )
    path = write_config(tmp_path, sqlite_config())

    code, _, stderr = run(
        capsys, ["source", "update", DATASOURCE_ID, "--config", str(path)]
    )

    assert code == 2
    assert updating.call_count == 0
    assert service.scan_datasource.call_count == 0
    assert "name" in stderr


@pytest.mark.parametrize(
    ("existing", "field"),
    [
        ({"name": "warehouse"}, "name"),
        ({"kind": "document"}, "kind"),
        ({"workspace_id": "other"}, "workspace_id"),
    ],
)
def test_update_refuses_each_immutable_field_independently(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    existing: dict[str, Any],
    field: str,
) -> None:
    updating = Mock()
    serving(
        monkeypatch,
        inspect_datasource=Mock(return_value=detail(**existing)),
        update_secure_datasource=updating,
    )
    path = write_config(tmp_path, sqlite_config())

    code, _, stderr = run(capsys, ["source", "update", DATASOURCE_ID, "--config", str(path)])

    assert code == 2
    assert updating.call_count == 0
    assert field in stderr


def test_update_of_a_missing_datasource_fails_before_any_write(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    updating = Mock()
    serving(
        monkeypatch,
        inspect_datasource=Mock(side_effect=DatasourceNotFoundError("gone")),
        update_secure_datasource=updating,
    )
    path = write_config(tmp_path, sqlite_config())

    code, stdout, _ = run(
        capsys, ["source", "update", DATASOURCE_ID, "--config", str(path), "--json"]
    )

    assert code == 1
    assert updating.call_count == 0
    assert json.loads(stdout)["error"]["code"] == "datasource_not_found"


def test_update_calls_exactly_two_service_methods_in_order(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Read-then-write, and nothing else: no scan, no test, no second write."""
    service = serving(
        monkeypatch,
        inspect_datasource=Mock(return_value=detail()),
        update_secure_datasource=Mock(return_value=datasource(status="created")),
    )
    path = write_config(tmp_path, sqlite_config())

    run(capsys, ["source", "update", DATASOURCE_ID, "--config", str(path)])

    assert [name for name, _, _ in service.mock_calls] == [
        "inspect_datasource",
        "update_secure_datasource",
    ]
    assert service.scan_datasource.call_count == 0
    assert service.test_secure_datasource.call_count == 0
    assert service.mock_calls[0] == call.inspect_datasource(DATASOURCE_ID)
    update_id, update_request = service.mock_calls[1].args
    assert update_id == DATASOURCE_ID
    assert update_request.connection_profile.driver == "sqlite"


# --------------------------------------------------------------------------------------------
# source delete
# --------------------------------------------------------------------------------------------


def test_delete_calls_delete_once_and_has_no_force_flag(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    deleting = Mock()
    service = serving(monkeypatch, delete_datasource=deleting)

    code, stdout, stderr = run(capsys, ["source", "delete", DATASOURCE_ID])

    assert code == 0
    assert stderr == ""
    assert deleting.call_args.args == (DATASOURCE_ID,)
    assert "[PASS] Datasource deleted" in stdout
    assert service.scan_datasource.call_count == 0

    with pytest.raises(SystemExit) as raised:
        cli.main(["source", "delete", DATASOURCE_ID, "--force"])
    assert raised.value.code == 2


def test_delete_json_reports_the_id_only(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    serving(monkeypatch, delete_datasource=Mock())

    code, stdout, _ = run(capsys, ["source", "delete", DATASOURCE_ID, "--json"])

    assert code == 0
    assert json.loads(stdout) == {"status": "completed", "datasource_id": DATASOURCE_ID}


def test_delete_of_a_missing_datasource_exits_one(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    serving(monkeypatch, delete_datasource=Mock(side_effect=DatasourceNotFoundError("gone")))

    code, stdout, _ = run(capsys, ["source", "delete", DATASOURCE_ID, "--json"])

    assert code == 1
    assert json.loads(stdout)["error"]["code"] == "datasource_not_found"


# --------------------------------------------------------------------------------------------
# scan-one is unchanged
# --------------------------------------------------------------------------------------------


def test_scan_one_still_scans_and_reports_the_new_version(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The new secure commands must not have moved the existing projection's behaviour."""
    scanning = Mock(
        return_value=[DatasetInfo(datasource_id=DATASOURCE_ID, name="orders", kind="table")]
    )
    serving(
        monkeypatch,
        scan_datasource=scanning,
        inspect_datasource=Mock(
            return_value=detail(status="ready", scan_version=4, last_scan_status="ready")
        ),
    )

    code, stdout, _ = run(capsys, ["source", "scan-one", DATASOURCE_ID, "--json"])

    assert code == 0
    assert scanning.call_args.args == (DATASOURCE_ID,)
    document = json.loads(stdout)
    assert document["scan_version"] == 4
    assert document["dataset_count"] == 1


# --------------------------------------------------------------------------------------------
# config safety
# --------------------------------------------------------------------------------------------


def test_a_single_secure_config_is_accepted(
    tmp_path: Path,
) -> None:
    from smartdata.cli.config import load_single_datasource_request

    path = write_config(tmp_path, sqlite_config())

    request = load_single_datasource_request(path)

    assert request.name == "sales"
    assert request.workspace_id == "default"


def test_a_config_with_two_datasources_is_a_usage_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A single-datasource command must not silently pick one of several connections."""
    creating = Mock()
    serving(monkeypatch, create_secure_datasource=creating)
    path = write_config(tmp_path, [sqlite_config(), sqlite_config()])

    code, _, stderr = run(capsys, ["source", "create", "--config", str(path)])

    assert code == 2
    assert creating.call_count == 0
    assert "exactly one datasource" in stderr


def test_an_inline_password_is_refused_without_echoing_it(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The config is not a secret channel: an inline password is refused, not stored."""
    creating = Mock()
    serving(monkeypatch, create_secure_datasource=creating)
    payload = sqlite_config(
        authentication={"method": "password", "username": "ro", "password": SECRET_PASSWORD}
    )
    path = write_config(tmp_path, payload)

    code, stdout, stderr = run(capsys, ["source", "create", "--config", str(path)])

    assert code == 2
    assert creating.call_count == 0
    assert SECRET_PASSWORD not in stderr
    assert SECRET_PASSWORD not in stdout


def test_a_password_inside_a_url_is_refused(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    creating = Mock()
    serving(monkeypatch, create_secure_datasource=creating)
    payload = sqlite_config(endpoint={"url": SECRET_URL})
    path = write_config(tmp_path, payload)

    code, _, stderr = run(capsys, ["source", "create", "--config", str(path)])

    assert code == 2
    assert creating.call_count == 0
    assert SECRET_PASSWORD not in stderr


@pytest.mark.parametrize("provider", ["managed", "environment", "file"])
def test_every_secret_reference_provider_is_accepted(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider: str,
) -> None:
    """A reference is the supported way to point at a secret; only an inline value is refused."""
    creating = Mock(return_value=datasource())
    serving(monkeypatch, create_secure_datasource=creating)
    payload = sqlite_config(
        authentication={
            "method": "password",
            "username": "ro",
            "password": {"provider": provider, "identifier": "SMARTDATA_DB_PASSWORD"},
        }
    )
    path = write_config(tmp_path, payload)

    code, _, _ = run(capsys, ["source", "create", "--config", str(path)])

    assert code == 0
    assert creating.call_count == 1


def test_a_config_file_that_is_missing_is_a_usage_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    creating = Mock()
    serving(monkeypatch, create_secure_datasource=creating)

    code, _, _ = run(
        capsys, ["source", "create", "--config", str(tmp_path / "absent.json")]
    )

    assert code == 2
    assert creating.call_count == 0


def test_the_secure_commands_do_not_add_a_second_update_config_format() -> None:
    """Update reuses ``SecureDatasourceUpdate``: no CLI-only profile model exists."""
    source = inspect.getsource(source_cli)
    assert "SecureDatasourceUpdate" in source
    assert "load_single_datasource_request" in source


def test_the_secure_commands_reach_nothing_but_the_application_service() -> None:
    source = inspect.getsource(source_cli.run_secure_command)
    source += inspect.getsource(source_cli._run_secure_update)
    for name in ("Catalog(", "SmartDataService(", "TLSMaterializer", "CertificateValidator"):
        assert name not in source, name


# --------------------------------------------------------------------------------------------
# datasource config error type
# --------------------------------------------------------------------------------------------


def test_the_identity_mismatch_uses_the_shared_config_error() -> None:
    """A refused update is a config/usage problem, not a partial application."""
    assert issubclass(DatasourceConfigError, ValueError)
