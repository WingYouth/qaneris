from __future__ import annotations

"""The formal ``smartdata ask`` product command.

One command maps to one :class:`AskRequest` and one :class:`AskResponse`. The command is a thin
boundary over ``SmartDataService.ask()``: it owns argument parsing, exit codes and rendering, and
it never reaches into the intent parser, the grounder, the planner, the compiler, the executor or
the model gateway.

``--stream`` consumes the unified progress contract instead: one command maps to one
:class:`AskRequest` and a sequence of :class:`AskEvent` values produced by
``SmartDataService.ask_stream()``. The CLI owns no event schema of its own - it serializes the
contract's own events - and it never invents a stage that the pipeline did not report.
"""

import argparse
import json
import sys
from collections.abc import Sequence
from contextlib import nullcontext, redirect_stdout
from typing import Any, TextIO

from pydantic import ValidationError

from smartdata.cli.render import render_table
from smartdata.cli.source import application_service
from smartdata.common.errors import SmartDataError
from smartdata.common.redaction import SecretRedactor
from smartdata.contracts.api import AskRequest, AskResponse, AskStatus
from smartdata.contracts.ask_events import AskEvent, AskEventType

ASK_COMMAND = "ask"

#: Exit codes follow the existing CLI convention: the command completed when the pipeline produced
#: a verdict. A clarification is such a verdict - the API and the MCP surface both return it as a
#: normal product response, so it is not a runtime failure.
_EXIT_OK = 0
_EXIT_FAILED = 1
_EXIT_USAGE = 2

#: The machine projection of one AskResponse: a stable field selection, not a second source of
#: truth. ``analysis`` and ``recommended_questions`` are deliberately excluded - ``analysis``
#: mixes deterministic summaries with the intent model's own ambiguity notes, and neither belongs
#: to the product contract this command publishes.
_JSON_FIELDS = (
    "status",
    "question",
    "answer",
    "business_query",
    "clarification",
    "plan",
    "result",
    "evidence",
    "error",
)

#: One human stage line per contract event. The label and the headline are presentation; the fact
#: that a line appears at all is decided by the pipeline, so the CLI cannot announce a stage the
#: unified orchestration never reached.
_STREAM_STAGES: dict[AskEventType, str] = {
    AskEventType.ACCEPTED: "accepted",
    AskEventType.INTENT_READY: "intent",
    AskEventType.RETRIEVAL_READY: "retrieval",
    AskEventType.GROUNDING_READY: "grounding",
    AskEventType.CLARIFICATION_REQUIRED: "clarification",
    AskEventType.PLAN_READY: "plan",
    AskEventType.QUERY_READY: "query",
    AskEventType.EXECUTION_STARTED: "execution",
    AskEventType.RESULT_READY: "result",
    AskEventType.ERROR: "error",
    AskEventType.DONE: "done",
}

_STREAM_HEADLINES: dict[AskEventType, str] = {
    AskEventType.ACCEPTED: "Request accepted",
    AskEventType.INTENT_READY: "Intent ready",
    AskEventType.RETRIEVAL_READY: "Retrieval ready",
    AskEventType.GROUNDING_READY: "Grounding ready",
    AskEventType.CLARIFICATION_REQUIRED: "Clarification required",
    AskEventType.PLAN_READY: "Query plan ready",
    AskEventType.QUERY_READY: "Query ready",
    AskEventType.EXECUTION_STARTED: "Executing",
    AskEventType.RESULT_READY: "Result ready",
    AskEventType.ERROR: "Ask failed",
    AskEventType.DONE: "Completed",
}

#: A terminal ``done`` reports the pipeline's own verdict rather than a fixed word, so a failed or
#: clarified run cannot be mistaken for a completed one.
_STREAM_DONE_HEADLINES: dict[str, str] = {
    AskStatus.COMPLETED.value: "Completed",
    AskStatus.CLARIFICATION_REQUIRED.value: "Clarification required",
    AskStatus.FAILED.value: "Failed",
}


def _datasource_id(value: str) -> str:
    datasource = value.strip()
    if not datasource:
        raise argparse.ArgumentTypeError("must not be empty")
    return datasource


def add_ask_command(commands: argparse._SubParsersAction) -> None:
    ask = commands.add_parser(
        "ask", help="ask one natural-language question through the Ask pipeline"
    )
    ask.add_argument("question", metavar="QUESTION", help="the question to ask")
    ask.add_argument(
        "--workspace",
        dest="workspace_id",
        default="default",
        help="workspace id (default: default)",
    )
    ask.add_argument(
        "--datasource",
        dest="datasource_id",
        type=_datasource_id,
        help="datasource id to scope the question to (an id, never a name)",
    )
    ask.add_argument(
        "--max-rows",
        dest="max_rows",
        type=int,
        help="row limit for the result (validated by the AskRequest contract)",
    )
    ask.add_argument(
        "--stream",
        action="store_true",
        dest="stream_output",
        help="consume the unified Ask event stream instead of the one-shot response",
    )
    ask.add_argument("--json", action="store_true", dest="json_output")


def build_ask_request(args: argparse.Namespace) -> AskRequest:
    """Map parsed arguments onto the formal request contract.

    ``workspace_id`` is stripped here so an all-whitespace value is a usage error, and
    ``max_rows`` is left out entirely when not given so the contract default applies. The allowed
    range of ``max_rows`` is owned by ``AskRequest`` - the CLI does not copy the numbers.
    """
    workspace = args.workspace_id.strip() if args.workspace_id else ""
    if not workspace:
        raise ValueError("workspace must not be empty")
    values: dict[str, Any] = {"question": args.question, "workspace_id": workspace}
    if args.datasource_id is not None:
        values["datasource_id"] = args.datasource_id
    if args.max_rows is not None:
        values["max_rows"] = args.max_rows
    return AskRequest(**values)


def ask_payload(response: AskResponse) -> dict[str, Any]:
    dumped = response.model_dump(mode="json")
    return {name: dumped[name] for name in _JSON_FIELDS}


def _plan_summary(plan: Any) -> list[str]:
    """A short, safe plan digest: object names, group-by paths and aggregate aliases."""
    lines: list[str] = []
    objects = getattr(plan, "data_objects", None)
    if objects:
        names = [
            locator.name or locator.qualified_name or object_id
            for object_id, locator in objects.items()
        ]
        lines.append(f"Objects: {', '.join(names)}")
    group_by = getattr(plan, "group_by", None)
    if group_by:
        lines.append(f"Group by: {', '.join(ref.field_path for ref in group_by)}")
    aggregates = getattr(plan, "aggregates", None)
    if aggregates:
        parts = []
        for aggregate in aggregates:
            field = aggregate.field.field_path if aggregate.field is not None else "*"
            parts.append(f"{aggregate.function.value}({field}) AS {aggregate.alias}")
        lines.append(f"Aggregates: {', '.join(parts)}")
    return lines


def _result_summary(result: Any) -> tuple[list[str], str | None, int | None, bool, bool]:
    columns = list(getattr(result, "columns", []) or [])
    rows = list(getattr(result, "rows", []) or [])
    row_count = getattr(result, "row_count", None)
    truncated = bool(getattr(result, "truncated", False))
    return columns, rows, row_count, truncated, row_count is not None


def print_ask_report(response: AskResponse, *, json_output: bool) -> int:
    """Print one AskResponse and return the command's exit code."""
    if json_output:
        print(json.dumps(ask_payload(response), ensure_ascii=False, indent=2))
        return _EXIT_OK if response.status is not AskStatus.FAILED else _EXIT_FAILED

    if response.status is AskStatus.CLARIFICATION_REQUIRED:
        print("Clarification required")
        for item in response.clarification:
            print(f"Question: {item.question}")
            for option in item.options:
                print(f"  - {option}")
        if not response.clarification and response.answer:
            print(f"Question: {response.answer}")
        return _EXIT_OK

    if response.status is AskStatus.FAILED:
        # Product-level failure: reported on stderr like every other failed operation, without a
        # traceback and without an unredacted message.
        code = response.error.code if response.error is not None else "ask_failed"
        message = response.error.message if response.error is not None else response.answer
        print(f"Status: failed\nError: {code}: {message}", file=sys.stderr)
        return _EXIT_FAILED

    print("Status: completed")
    if response.answer:
        print(f"Answer: {response.answer}")
    result = response.result
    evidence = response.evidence
    datasource = getattr(evidence, "datasource_id", None) or getattr(
        result, "datasource_id", None
    )
    if datasource:
        print(f"Datasource: {datasource}")
    scan_version = getattr(evidence, "scan_version", None)
    if scan_version is not None:
        print(f"Scan version: {scan_version}")
    columns, rows, row_count, truncated, known = _result_summary(result)
    if known:
        print(f"Rows: {row_count if row_count is not None else len(rows)}")
        print(f"Truncated: {'yes' if truncated else 'no'}")

    if columns:
        print("\nResult:")
        for line in render_table(columns, rows):
            print(line)

    if evidence is not None:
        print("\nQuery:")
        print(evidence.display_command)

    plan_lines = _plan_summary(response.plan)
    if plan_lines:
        print("\nPlan:")
        for line in plan_lines:
            print(line)
    return _EXIT_OK


def print_ask_usage_error(args: argparse.Namespace, error: Exception, *, json_output: bool) -> None:
    """Report an unusable request as a usage error, keeping the message on stderr.

    In streaming JSONL mode stdout belongs exclusively to ``AskEvent`` lines and a usage error
    happens before any event exists, so the document is reported on stderr there instead of on
    stdout. The exit code contract does not change.
    """
    if json_output:
        pure_stdout = bool(getattr(args, "stream_output", False))
        print(
            json.dumps(
                {
                    "status": "usage_error",
                    "error": {"type": type(error).__name__, "message": str(error)},
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr if pure_stdout else sys.stdout,
        )
        return
    print(f"Usage error: {error}", file=sys.stderr)


def print_ask_error(args: argparse.Namespace, error: Exception, *, json_output: bool) -> None:
    """Report a product-level failure (datasource scope, model gateway) without a traceback."""
    message = SecretRedactor.from_environment().text(error.message) if isinstance(
        error, SmartDataError
    ) else type(error).__name__
    if json_output:
        print(
            json.dumps(
                {
                    "status": "operation_failed",
                    "error": {
                        "type": type(error).__name__,
                        "code": getattr(error, "code", None),
                        "message": message,
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    print(f"Ask failed: {message}", file=sys.stderr)


def _stream_facts(pairs: Sequence[tuple[str, Any]]) -> str:
    """Render the ``(key=value, ...)`` suffix of a stage line from facts the event really carries.

    A fact whose value is absent is dropped rather than printed as a placeholder, so a stage line
    never claims more than the contract event reported.
    """
    rendered = [f"{key}={value}" for key, value in pairs if value is not None]
    return f" ({', '.join(rendered)})" if rendered else ""


def _stream_stage_lines(event: AskEvent) -> list[str]:
    """Turn one contract event into the human stage line(s) it stands for."""
    payload = event.payload
    label = _STREAM_STAGES[event.event_type]
    headline = _STREAM_HEADLINES[event.event_type]
    event_type = event.event_type

    if event_type is AskEventType.DONE:
        headline = _STREAM_DONE_HEADLINES.get(str(payload.get("status")), headline)
        return [f"[{label}] {headline}"]
    if event_type is AskEventType.ERROR:
        error = payload.get("error")
        detail = error if isinstance(error, dict) else {}
        code = detail.get("code") or "ask_failed"
        message = detail.get("message") or ""
        detail_text = f"{code}: {message}" if message else str(code)
        return [f"[{label}] {headline}: {detail_text}"]
    if event_type is AskEventType.RETRIEVAL_READY:
        suffix = _stream_facts((("candidates", payload.get("candidate_count")),))
    elif event_type is AskEventType.GROUNDING_READY:
        executable = "yes" if payload.get("is_executable") else "no"
        suffix = _stream_facts((("executable", executable),))
    elif event_type is AskEventType.PLAN_READY:
        plan = payload.get("plan")
        plan_id = plan.get("plan_id") if isinstance(plan, dict) else None
        suffix = _stream_facts((("plan_id", plan_id),))
    elif event_type is AskEventType.QUERY_READY:
        suffix = _stream_facts((("language", payload.get("query_language")),))
    elif event_type is AskEventType.EXECUTION_STARTED:
        suffix = _stream_facts(
            (
                ("datasource", payload.get("datasource_id")),
                ("scan_version", payload.get("scan_version")),
            )
        )
    elif event_type is AskEventType.RESULT_READY:
        truncated = "yes" if payload.get("truncated") else "no"
        suffix = _stream_facts(
            (("rows", payload.get("row_count")), ("truncated", truncated))
        )
    else:
        suffix = ""

    lines = [f"[{label}] {headline}{suffix}"]
    if event_type is AskEventType.CLARIFICATION_REQUIRED:
        lines.extend(_clarification_lines(payload))
    return lines


def _clarification_lines(payload: dict[str, Any]) -> list[str]:
    """Render the public clarification the event carries - never a private reasoning note."""
    lines: list[str] = []
    items = payload.get("clarification")
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        question = item.get("question")
        if question:
            lines.append(f"  Question: {question}")
        for option in item.get("options") or []:
            lines.append(f"    - {option}")
    return lines


def _stream_result_lines(event: AskEvent) -> list[str]:
    """Render the completed body once, from the public response the event already carries.

    Nothing here reads a model, a graph or the database: the projection comes from the event
    payload, which the application service already filtered and redacted.
    """
    if event.event_type is not AskEventType.RESULT_READY:
        return []
    response = event.payload.get("response")
    if not isinstance(response, dict) or response.get("status") != AskStatus.COMPLETED.value:
        return []
    lines: list[str] = []
    answer = response.get("answer")
    if answer:
        lines.extend(["", f"Answer: {answer}"])
    result = response.get("result")
    result = result if isinstance(result, dict) else {}
    columns = list(result.get("columns") or [])
    rows = list(result.get("rows") or [])
    if columns:
        lines.extend(["", "Result:", *render_table(columns, rows)])
    evidence = response.get("evidence")
    command = evidence.get("display_command") if isinstance(evidence, dict) else None
    if command:
        lines.extend(["", "Query:", str(command)])
    return lines


def print_ask_stream_event(event: AskEvent, *, json_output: bool, out: TextIO) -> None:
    """Emit one contract event as a JSONL line or as human stage lines.

    ``flush`` keeps the mode genuinely incremental: a caller watching a terminal sees a stage as
    soon as the pipeline reaches it instead of at process exit.
    """
    if json_output:
        # The line is the contract's own event, serialized by its own model: the CLI owns no
        # second event schema and adds no wrapper fields.
        print(event.model_dump_json(), file=out, flush=True)
        return
    for line in _stream_stage_lines(event):
        print(line, file=out, flush=True)
    for line in _stream_result_lines(event):
        print(line, file=out, flush=True)


def _stream_failed(event: AskEvent) -> bool:
    """True when an event is the stream's own failure signal.

    ``error`` is the pipeline's failure event, and the terminal ``done`` repeats the verdict in its
    payload. Nothing else marks a stream as failed - a clarification is a normal product result.
    """
    if event.event_type is AskEventType.ERROR:
        return True
    return (
        event.event_type is AskEventType.DONE
        and event.payload.get("status") == AskStatus.FAILED.value
    )


def run_ask_stream(args: argparse.Namespace, request: AskRequest) -> int:
    """Consume the unified Ask event stream and return the command's exit code.

    stdout carries the trace (or, with ``--json``, one contract event per line) and nothing else:
    incidental driver, provider or debug output is redirected to stderr for the whole iteration, so
    JSONL purity does not depend on the drivers being quiet. ``done`` is the last line the pipeline
    sends and the CLI neither reorders it nor appends anything after it.
    """
    service = application_service()
    out = sys.stdout
    failed = False
    try:
        with redirect_stdout(sys.stderr):
            for event in service.ask_stream(request):
                print_ask_stream_event(event, json_output=args.json_output, out=out)
                if _stream_failed(event):
                    failed = True
    except Exception as error:  # noqa: BLE001 - safe top-level CLI boundary
        # ``ask_stream`` reports a pipeline failure as ``error`` then ``done``. Reaching this handler
        # means the stream broke outside that contract, so the failure is reported on stderr without
        # fabricating an event to cover the gap.
        print_ask_error(args, error, json_output=False)
        return _EXIT_FAILED
    return _EXIT_FAILED if failed else _EXIT_OK


def run_ask_command(args: argparse.Namespace) -> int:
    """Run one ask through the only Application Service entry point."""
    try:
        request = build_ask_request(args)
    except (ValidationError, ValueError) as error:
        # The range of --max-rows is owned by the AskRequest contract; a violation surfaces here
        # as an unusable request, which is a usage error (exit 2), not a product failure.
        print_ask_usage_error(args, error, json_output=args.json_output)
        return _EXIT_USAGE

    if args.stream_output:
        # Streaming consumes the same request object, so both modes validate arguments identically.
        return run_ask_stream(args, request)

    service = application_service()
    response: AskResponse | None = None
    failure: Exception | None = None
    # The structured output is emitted below; any incidental driver or provider output the
    # pipeline prints on the way must not land inside the JSON document. Error reports are
    # printed after the redirect closes so a JSON error document lands on stdout, not stderr.
    with redirect_stdout(sys.stderr) if args.json_output else nullcontext():
        try:
            response = service.ask(request)
        except Exception as error:  # noqa: BLE001 - safe top-level CLI boundary
            failure = error
    if failure is not None:
        # Product errors keep their redacted message; unexpected exceptions expose only their
        # type, never an arbitrary message that could carry a provider credential.
        print_ask_error(args, failure, json_output=args.json_output)
        return _EXIT_FAILED
    assert response is not None
    return print_ask_report(response, json_output=args.json_output)
