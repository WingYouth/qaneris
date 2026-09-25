from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from smartdata.acceptance.phase1 import (
    PHASE1_SCENARIOS,
    Phase1AcceptanceRunner,
    Phase1RunReport,
    ScenarioResult,
)
from smartdata.application.service import SmartDataService
from smartdata.catalog import Catalog
from smartdata.cli import main as cli
from smartdata.contracts import BusinessQuery
from smartdata.graph.ports import NullGraphReader, NullGraphStore
from smartdata.scripts.acceptance import phase1_e2e
from smartdata.semantic.models import SemanticRetrievalResult


def report(tmp_path: Path, *, status: str = "passed") -> Phase1RunReport:
    result_status = "passed" if status == "passed" else "failed"
    result = ScenarioResult(
        code="A",
        title="Single Metric",
        question="总实收销售额是多少？",
        status=result_status,
    )
    return Phase1RunReport(
        run_id="run-test",
        started_at="2026-01-01T00:00:00Z",
        completed_at="2026-01-01T00:00:01Z",
        status=status,
        environment_summary={},
        suite="phase1",
        results=[result],
        summary={
            "total": 1,
            "passed": int(status == "passed"),
            "failed": int(status != "passed"),
            "blocked": 0,
        },
        artifact_paths={
            "report_json": str(tmp_path / "report.json"),
            "report_markdown": str(tmp_path / "report.md"),
        },
    )


@pytest.mark.parametrize(
    "arguments",
    [
        ["--help"],
        ["doctor", "--help"],
        ["acceptance", "--help"],
        ["acceptance", "phase1", "--help"],
        ["acceptance", "ask", "--help"],
    ],
)
def test_command_help(arguments, capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(arguments)

    assert raised.value.code == 0
    assert "usage:" in capsys.readouterr().out


def test_json_stdout_is_pure_and_options_reach_runner(tmp_path, capsys, monkeypatch) -> None:
    expected = report(tmp_path)
    def noisy_run(**kwargs):
        print("dependency diagnostic")
        return expected

    runner = SimpleNamespace(run=Mock(side_effect=noisy_run))
    monkeypatch.setattr(cli, "Phase1AcceptanceRunner", lambda: runner)

    exit_code = cli.main(
        [
            "acceptance",
            "ask",
            "--suite",
            "phase1",
            "--question-class",
            "c",
            "--repeat",
            "3",
            "--no-trace",
            "--json",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == "dependency diagnostic\n"
    assert exit_code == 0
    assert payload["run_id"] == "run-test"
    runner.run.assert_called_once_with(question_class="C", repeat=3, trace=False)


def test_failed_acceptance_has_exit_code_one(tmp_path, capsys, monkeypatch) -> None:
    runner = SimpleNamespace(run=Mock(return_value=report(tmp_path, status="failed")))
    monkeypatch.setattr(cli, "Phase1AcceptanceRunner", lambda: runner)

    assert cli.main(["acceptance", "phase1"]) == 1
    assert "[FAIL]" in capsys.readouterr().out


def test_json_configuration_error_is_structured(tmp_path, capsys) -> None:
    exit_code = cli.main(
        ["--env-file", str(tmp_path / "missing.env"), "doctor", "--json"]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 2
    assert payload["status"] == "configuration_error"
    assert payload["error"]["type"] == "EnvironmentBootstrapError"
    assert captured.err == ""


def test_doctor_json_contains_statuses_only(capsys, monkeypatch) -> None:
    checks = {
        "artifact_root": {"status": "available"},
        "model_configuration": {"status": "missing"},
    }
    monkeypatch.setattr(cli, "inspect_environment", lambda: checks)

    assert cli.main(["doctor", "--json"]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"checks": checks}
    assert captured.err == ""


def test_missing_model_creates_blocked_artifacts(tmp_path, monkeypatch) -> None:
    for key in list(sys.modules["os"].environ):
        if key.startswith("SMARTDATA_MODEL_"):
            monkeypatch.delenv(key, raising=False)
    runner = Phase1AcceptanceRunner(artifact_base=tmp_path / "SmartDataArtifacts")

    actual = runner.run(question_class="A", repeat=3, trace=False)

    assert actual.status == "environment_blocked"
    assert actual.results[0].status == "environment_blocked"
    assert len(actual.results) == 3
    assert actual.summary == {"total": 3, "passed": 0, "failed": 0, "blocked": 3}
    json_path = Path(actual.artifact_paths["report_json"])
    markdown_path = Path(actual.artifact_paths["report_markdown"])
    assert json_path.is_file()
    assert markdown_path.is_file()
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["run_id"] == actual.run_id
    assert "# Phase 1 E2E Acceptance" in markdown_path.read_text(encoding="utf-8")


def test_runner_redacts_secrets_from_reports(tmp_path, monkeypatch) -> None:
    secret = "unit-super-secret"
    monkeypatch.setenv("SMARTDATA_MODEL_API_KEY", secret)
    runner = Phase1AcceptanceRunner(artifact_base=tmp_path / "SmartDataArtifacts")
    monkeypatch.setattr(
        runner,
        "_prepare",
        Mock(side_effect=RuntimeError(f"provider rejected {secret}")),
    )

    actual = runner.run(question_class="A", trace=False)
    artifact_text = Path(actual.artifact_paths["report_json"]).read_text(encoding="utf-8")
    markdown_text = Path(actual.artifact_paths["report_markdown"]).read_text(encoding="utf-8")

    assert secret not in artifact_text
    assert secret not in markdown_text
    assert "<redacted>" in artifact_text


def test_expected_clarification_is_a_pass() -> None:
    scenario = next(item for item in PHASE1_SCENARIOS if item.code == "E")
    result = ScenarioResult(
        code="E",
        title=scenario.title,
        question=scenario.question,
        ask_status="clarification_required",
        clarification=[{"question": "请选择营业额口径", "options": ["实收", "含税"]}],
    )

    assert Phase1AcceptanceRunner._passes(scenario, result) is True


def test_legacy_script_delegates_to_shared_runner(tmp_path, capsys, monkeypatch) -> None:
    expected = report(tmp_path)
    run = Mock(return_value=expected)
    monkeypatch.setattr(phase1_e2e, "Phase1AcceptanceRunner", lambda: SimpleNamespace(run=run))
    monkeypatch.setattr(phase1_e2e, "load_runtime_environment", Mock())
    monkeypatch.setattr(sys, "argv", ["phase1_e2e", "--question-class", "B", "--no-trace"])

    assert phase1_e2e.main() == 0
    run.assert_called_once_with(question_class="B", repeat=1, trace=False)
    assert "1/1 passed" in capsys.readouterr().out


class _IntentModel:
    def parse_business_query(self, question: str, rule_facts: dict) -> BusinessQuery:
        return BusinessQuery(
            question=question,
            objective="lookup",
            metrics=["营业额"],
            confidence=0.4,
        )

    def plan_query(self, *args, **kwargs):  # pragma: no cover - architectural guard
        raise AssertionError("model must not plan")

    def answer_question(self, *args, **kwargs):  # pragma: no cover - architectural guard
        raise AssertionError("model must not answer")


def test_cli_runner_calls_real_smartdata_service_ask(tmp_path, capsys, monkeypatch) -> None:
    service = SmartDataService(
        Catalog(tmp_path / "catalog.db"),
        model=_IntentModel(),
        graph_store=NullGraphStore(),
        graph_reader=NullGraphReader(),
    )
    service._require_ready_scope = Mock()
    # Low confidence now proceeds to retrieval. An empty published scope produces a concrete
    # missing-metric clarification through the real grounder, without requiring a graph service.
    service.retrieve_semantics = Mock(
        side_effect=lambda query, **kwargs: SemanticRetrievalResult(
            business_query=query,
            candidates=[],
            requested_datasource_id="ds-test",
        )
    )
    ask = Mock(wraps=service.ask)
    service.ask = ask
    runner = Phase1AcceptanceRunner(artifact_base=tmp_path / "SmartDataArtifacts")
    monkeypatch.setattr(runner, "_prepare", lambda run_directory, report: (service, "ds-test"))
    monkeypatch.setattr(cli, "Phase1AcceptanceRunner", lambda: runner)

    exit_code = cli.main(
        ["acceptance", "phase1", "--question-class", "E", "--no-trace", "--json"]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["results"][0]["status"] == "passed"
    assert payload["results"][0]["ask_status"] == "clarification_required"
    ask.assert_called_once()
