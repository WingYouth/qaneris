"""HTTP SSE transport tests for the unified Ask event stream (RS-STREAM-01B).

The events under test are produced by the *real* Application Service orchestration - a stand-in
intent model plus mocked physical stages, exactly as ``tests/unit/application/test_ask_stream.py``
wires them - and then carried through the *real* endpoint. That is what makes it possible to assert
the transport is transparent: every frame a client would receive is compared, field by field, with
the event the service actually emitted.

Every response is parsed as SSE rather than probed with substring assertions. A frame that is
malformed, mislabelled, missing its payload field, or padded with a transport-invented field fails
here, and each ``data`` document is re-validated as ``AskEvent`` - which is what proves the endpoint
publishes no second schema.

The pipeline itself (intent, retrieval, grounding, planning, validation, execution) is covered by
the Core suites and is deliberately not retested here.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from qaneris.application.service import QanerisService
from qaneris.catalog import Catalog
from qaneris.common.errors import QanerisError, QueryExecutionError
from qaneris.contracts import (
    AskEvent,
    AskEventType,
    AskRequest,
    AskResponse,
    AskStatus,
    BusinessQuery,
    ExecutionEvidence,
    GroundedExecution,
    GroundedQueryPlan,
    GroundedQueryResult,
    PlanAggregate,
    QueryLanguage,
    QueryResultType,
)
from qaneris.contracts.semantic import AggregateFunction, SemanticAssetType
from qaneris.interfaces.api import app as api_app
from qaneris.interfaces.api import ask_stream as transport
from qaneris.interfaces.api.app import create_app
from qaneris.semantic import ClarificationOption, ClarificationRequest

ENDPOINT = "/api/ask/stream"
SYNC_ENDPOINT = "/api/ask"
EXCEL_ENDPOINT = "/api/datasources/import-excel"

QUESTION = "按地区看销售额"
WORKSPACE_ID = "workspace-a"
DATASOURCE_ID = "ds-sales"

# A credential-shaped token that must never reach the wire.
SECRET_TOKEN = "sk-transport-secret-token"

#: The five fields the event contract publishes. The transport may not add or rename one.
EVENT_FIELDS = {"event_type", "sequence", "payload", "occurred_at", "correlation_id"}

ASK_BODY = {
    "question": QUESTION,
    "workspace_id": WORKSPACE_ID,
    "datasource_id": DATASOURCE_ID,
}


# --------------------------------------------------------------------------------------------
# the real orchestration, wired deterministically
# --------------------------------------------------------------------------------------------


class IntentModel:
    """A stand-in business-intent parser with an unavailable answer model."""

    def __init__(self, query: BusinessQuery):
        self.query = query

    def parse_business_query(self, question: str, rule_facts: dict) -> BusinessQuery:
        return self.query

    def plan_query(self, *args, **kwargs):
        raise AssertionError("intent model must not plan queries")

    def answer_question(self, *args, **kwargs):
        raise AssertionError("answer model unavailable")


def business_query(**changes: Any) -> BusinessQuery:
    values: dict[str, Any] = {
        "question": QUESTION,
        "objective": "lookup",
        "metrics": ["销售额"],
        "dimensions": ["地区"],
    }
    values.update(changes)
    return BusinessQuery(**values)


def grounded_plan() -> GroundedQueryPlan:
    return GroundedQueryPlan(
        plan_id="plan-stream",
        workspace_id=WORKSPACE_ID,
        datasource_id=DATASOURCE_ID,
        data_object_ids=["orders"],
        data_objects={
            "orders": {
                "datasource_id": DATASOURCE_ID,
                "data_object_id": "orders",
                "name": "orders",
            }
        },
        scan_version=7,
        aggregates=[PlanAggregate(function=AggregateFunction.COUNT, alias="count_rows")],
        expected_result_type=QueryResultType.SCALAR,
    )


def grounded_execution() -> GroundedExecution:
    return GroundedExecution(
        result=GroundedQueryResult(
            plan_id="plan-stream",
            datasource_id=DATASOURCE_ID,
            query_language=QueryLanguage.SQL,
            columns=["count_rows"],
            rows=[{"count_rows": 2}],
            row_count=1,
            scan_version=7,
        ),
        evidence=ExecutionEvidence(
            plan_id="plan-stream",
            datasource_id=DATASOURCE_ID,
            scan_version=7,
            query_language=QueryLanguage.SQL,
            display_command='SELECT COUNT(*) AS "count_rows" FROM "orders" LIMIT ?',
            row_count=1,
        ),
    )


def retrieval_result() -> SimpleNamespace:
    return SimpleNamespace(
        candidates=[],
        requested_datasource_id=DATASOURCE_ID,
        scan_version=7,
        retrieval_path="trusted",
    )


def executable_grounding() -> SimpleNamespace:
    return SimpleNamespace(
        needs_clarification=False,
        is_executable=True,
        clarifications=[],
        grounded_query=SimpleNamespace(
            bindings=[SimpleNamespace(
                business_term="销售额",
                asset_type=SemanticAssetType.METRIC,
                datasource_id=DATASOURCE_ID,
                data_object_id="orders",
                field_path="amount",
            )],
            unresolved_ambiguities=[],
            requested_datasource_id=DATASOURCE_ID,
            scan_version=7,
        ),
    )


def wire_success(service: QanerisService) -> None:
    """Wire the physical stages so the real orchestration produces a complete run."""
    from qaneris.contracts import Datasource, DatasourceKind

    service.catalog.get_datasource = Mock(return_value=(Datasource(
        id=DATASOURCE_ID, name="sales", workspace_id=WORKSPACE_ID,
        kind=DatasourceKind.RELATIONAL, driver="sqlite"), {}))
    service.retrieve_semantics = Mock(return_value=retrieval_result())
    service.ground_semantics = Mock(return_value=executable_grounding())
    service.build_query_context = Mock(return_value=object())
    service.plan_grounded_query = Mock(return_value=grounded_plan())
    service.execute_grounded_plan = Mock(return_value=grounded_execution())


def wire_grounding_clarification(service: QanerisService) -> None:
    service.retrieve_semantics = Mock(return_value=retrieval_result())
    service.ground_semantics = Mock(
        return_value=SimpleNamespace(
            needs_clarification=True,
            is_executable=False,
            clarifications=[
                ClarificationRequest(
                    clarification_id="metric",
                    field="metric",
                    question="请选择销售额口径",
                    options=[ClarificationOption(option_id="paid", label="实收销售额")],
                )
            ],
            grounded_query=SimpleNamespace(
                bindings=[],
                unresolved_ambiguities=["销售额口径不唯一"],
                requested_datasource_id=DATASOURCE_ID,
                scan_version=7,
            ),
        )
    )


def streaming_service(
    tmp_path: Any, query: BusinessQuery | None = None
) -> tuple[QanerisService, list[AskEvent]]:
    """The real Application Service, with the stream it emits recorded.

    Recording the events the service actually yields is what lets these tests compare the wire
    against the contract value by value instead of against a hand-built expectation.
    """
    service = QanerisService(
        Catalog(tmp_path / "catalog.db"), model=IntentModel(query or business_query())
    )
    service._require_ready_scope = Mock()
    recorded: list[AskEvent] = []
    emitted = service.ask_stream

    def recording_ask_stream(request: AskRequest):
        for event in emitted(request):
            recorded.append(event)
            yield event

    service.ask_stream = recording_ask_stream
    return service, recorded


def build_client(tmp_path: Any, service: Any, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """An app composed exactly like production, around this one Application Service.

    ``create_app`` composes its own service and the synchronous route closes over it, so the double
    is installed at the composition seam rather than on ``app.state`` alone. That way every route
    sees the same object: the closure-based ones and the ones that resolve ``app.state.service``
    through ``service_of``.
    """
    monkeypatch.setattr(api_app, "QanerisService", lambda *args, **kwargs: service)
    return TestClient(create_app(database_path=str(tmp_path / "catalog.db")))


@pytest.fixture
def compose(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A factory: ``compose(service)`` returns a client over that Application Service."""

    def build(service: Any) -> TestClient:
        return build_client(tmp_path, service, monkeypatch)

    return build


def running_client(
    tmp_path: Any, compose: Any
) -> tuple[TestClient, QanerisService, list[AskEvent]]:
    """A client whose service produces a complete, real run."""
    service, recorded = streaming_service(tmp_path)
    wire_success(service)
    return compose(service), service, recorded


# --------------------------------------------------------------------------------------------
# SSE parsing - a real parser, not a substring probe
# --------------------------------------------------------------------------------------------


def sse_body(response: Any) -> str:
    """Decode the response body as the UTF-8 the SSE specification mandates."""
    return response.content.decode("utf-8")


def parse_sse(body: str) -> list[tuple[str, str]]:
    """Parse an SSE body into ``(event name, data document)`` pairs.

    Strict on purpose: a stray line, a missing ``event``/``data`` field, an extra field (an
    ``id:``/``retry:``/``: keepalive`` line the transport invented) or a frame that is not
    terminated by a blank line all fail here.
    """
    assert body.endswith("\n\n"), "every frame must be terminated by a blank line"
    frames: list[tuple[str, str]] = []
    for block in body.split("\n\n"):
        if not block:
            continue
        lines = block.split("\n")
        assert len(lines) == 2, lines
        name, name_separator, event_name = lines[0].partition(": ")
        field, data_separator, data = lines[1].partition(": ")
        assert (name, name_separator, field, data_separator) == ("event", ": ", "data", ": "), lines
        frames.append((event_name, data))
    return frames


def read_stream(
    client: TestClient, body: dict[str, Any] | None = None
) -> tuple[Any, list[AskEvent]]:
    """POST one Ask and return the response plus the events parsed off the wire."""
    response = client.post(ENDPOINT, json=body if body is not None else ASK_BODY)
    frames = parse_sse(sse_body(response))
    events = [AskEvent.model_validate_json(data) for _, data in frames]
    for (name, _), event in zip(frames, events, strict=True):
        assert name == event.event_type.value
    return response, events


# --------------------------------------------------------------------------------------------
# endpoint contract
# --------------------------------------------------------------------------------------------


def test_the_endpoint_streams_events_with_the_event_stream_media_type(tmp_path: Any, compose: Any) -> None:
    client, _, _ = running_client(tmp_path, compose)

    response, events = read_stream(client)

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache"
    assert events


def test_the_request_uses_the_existing_ask_request_contract(tmp_path: Any, compose: Any) -> None:
    service, _ = streaming_service(tmp_path)
    wire_success(service)
    received: list[AskRequest] = []
    emitted = service.ask_stream

    def capture(request: AskRequest):
        received.append(request)
        yield from emitted(request)

    service.ask_stream = capture
    response = compose(service).post(
        ENDPOINT,
        json={
            "question": QUESTION,
            "workspace_id": "ws-1",
            "datasource_id": DATASOURCE_ID,
            "max_rows": 5,
        },
    )

    assert response.status_code == 200
    assert len(received) == 1
    assert isinstance(received[0], AskRequest)
    assert received[0].question == QUESTION
    assert received[0].workspace_id == "ws-1"
    assert received[0].datasource_id == DATASOURCE_ID
    assert received[0].max_rows == 5


def test_a_malformed_body_is_refused_before_the_stream_starts(tmp_path: Any, compose: Any) -> None:
    """FastAPI's own request validation refuses an unusable body; no event pair is manufactured."""
    service, recorded = streaming_service(tmp_path)
    wire_success(service)
    client = compose(service)

    empty = client.post(ENDPOINT, json={"question": ""})
    invalid_rows = client.post(ENDPOINT, json={"question": QUESTION, "max_rows": 0})

    assert empty.status_code == 422
    assert empty.json()["error"]["code"] == "validation_error"
    assert invalid_rows.status_code == 422
    assert recorded == []


# --------------------------------------------------------------------------------------------
# transparency: the wire is the contract
# --------------------------------------------------------------------------------------------


def test_a_successful_run_keeps_the_service_event_order(tmp_path: Any, compose: Any) -> None:
    client, _, recorded = running_client(tmp_path, compose)

    response, events = read_stream(client)

    assert response.status_code == 200
    assert [event.event_type.value for event in events] == [
        "accepted",
        "intent_ready",
        "retrieval_ready",
        "grounding_ready",
        "plan_ready",
        "query_ready",
        "execution_started",
        "result_ready",
        "done",
    ]
    assert events == recorded


def test_the_transport_changes_nothing_about_the_events_it_carries(tmp_path: Any, compose: Any) -> None:
    """Order, sequence, correlation id, timestamp and payload all survive the transport."""
    client, _, recorded = running_client(tmp_path, compose)

    _, events = read_stream(client)

    assert [event.event_type for event in events] == [item.event_type for item in recorded]
    assert [event.sequence for event in events] == [item.sequence for item in recorded]
    assert [event.correlation_id for event in events] == [item.correlation_id for item in recorded]
    assert [event.occurred_at for event in events] == [item.occurred_at for item in recorded]
    assert [event.payload for event in events] == [item.payload for item in recorded]
    assert events == recorded


def test_every_data_document_revalidates_as_the_published_event_contract(tmp_path: Any, compose: Any) -> None:
    """This is what proves the endpoint publishes no second, transport-only schema."""
    client, _, _ = running_client(tmp_path, compose)

    response = client.post(ENDPOINT, json=ASK_BODY)
    frames = parse_sse(sse_body(response))

    assert len(frames) == 9
    for name, data in frames:
        document = json.loads(data)
        assert set(document) == EVENT_FIELDS
        event = AskEvent.model_validate(document)
        assert event.event_type.value == name
        assert data == event.model_dump_json()  # the frame is the contract's own serialization


def test_the_sequence_and_the_correlation_id_are_the_service_ones(tmp_path: Any, compose: Any) -> None:
    client, _, recorded = running_client(tmp_path, compose)

    _, events = read_stream(client)

    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert len({event.correlation_id for event in events}) == 1
    assert events[0].correlation_id == recorded[0].correlation_id


def test_done_is_the_last_event_and_nothing_follows_it(tmp_path: Any, compose: Any) -> None:
    client, _, _ = running_client(tmp_path, compose)

    response = client.post(ENDPOINT, json=ASK_BODY)
    frames = parse_sse(sse_body(response))
    _, events = read_stream(client)

    assert frames[-1][0] == "done"
    assert events[-1].event_type is AskEventType.DONE
    assert [name for name, _ in frames[:-1]].count("done") == 0


def test_every_frame_is_one_event_and_the_framing_is_exact(tmp_path: Any, compose: Any) -> None:
    """The wire format itself: ``event: <type>`` then ``data: <json>`` then a blank line."""
    client, _, _ = running_client(tmp_path, compose)

    body = sse_body(client.post(ENDPOINT, json=ASK_BODY))

    assert body.endswith("\n\n")
    blocks = body.split("\n\n")[:-1]
    assert len(blocks) == 9
    for block in blocks:
        lines = block.split("\n")
        assert len(lines) == 2
        assert lines[0].startswith("event: ")
        assert lines[1].startswith("data: {")


# --------------------------------------------------------------------------------------------
# clarification is a normal product result
# --------------------------------------------------------------------------------------------


def test_low_confidence_streams_completed_result(tmp_path: Any, compose: Any) -> None:
    service, recorded = streaming_service(tmp_path, business_query(confidence=0.4))
    wire_success(service)
    client = compose(service)

    response, events = read_stream(client)

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream"
    assert [event.event_type.value for event in events] == [
        "accepted",
        "intent_ready",
        "retrieval_ready",
        "grounding_ready",
        "plan_ready",
        "query_ready",
        "execution_started",
        "result_ready",
        "done",
    ]
    assert events == recorded
    service.retrieve_semantics.assert_called_once()
    assert events[-1].payload["status"] == "completed"


def test_a_grounding_clarification_streams_and_carries_its_public_options(tmp_path: Any, compose: Any) -> None:
    service, recorded = streaming_service(tmp_path)
    wire_grounding_clarification(service)
    client = compose(service)

    response, events = read_stream(client)

    assert response.status_code == 200
    assert [event.event_type.value for event in events] == [
        "accepted",
        "intent_ready",
        "retrieval_ready",
        "grounding_ready",
        "clarification_required",
        "done",
    ]
    assert events == recorded
    clarification = events[-2].payload["clarification"][0]
    assert clarification["question"] == "请选择销售额口径"
    assert clarification["options"] == ["实收销售额"]


# --------------------------------------------------------------------------------------------
# a product failure is not a transport failure
# --------------------------------------------------------------------------------------------


def test_a_product_error_streams_as_error_then_done_on_an_http_200(tmp_path: Any, compose: Any) -> None:
    service, recorded = streaming_service(tmp_path)
    service.retrieve_semantics = Mock(side_effect=QueryExecutionError("database unavailable"))
    client = compose(service)

    response, events = read_stream(client)

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream"
    assert [event.event_type.value for event in events] == [
        "accepted",
        "intent_ready",
        "error",
        "done",
    ]
    assert events == recorded
    assert events[-2].payload["error"]["code"] == "execution_failed"
    assert events[-1].payload["status"] == "failed"


def test_the_verdict_is_not_rewritten_into_an_http_error(tmp_path: Any, compose: Any) -> None:
    """A stream that already started cannot become a 4xx/5xx; the verdict travels as an event."""
    service, _ = streaming_service(tmp_path)
    service.retrieve_semantics = Mock(side_effect=QanerisError("graph is not configured"))
    client = compose(service)

    response = client.post(ENDPOINT, json=ASK_BODY)

    assert response.status_code == 200
    assert response.status_code not in {400, 404, 422, 500}
    frames = parse_sse(sse_body(response))
    assert json.loads(frames[-2][1])["payload"]["error"]["code"] == "qaneris_error"


# --------------------------------------------------------------------------------------------
# the security boundary is the event contract, and the transport does not reopen it
# --------------------------------------------------------------------------------------------


class StubService:
    """A service double for the boundary cases the real orchestration cannot be asked to produce."""

    def __init__(self, events: list[AskEvent]):
        self.events = events

    def ask_stream(self, request: AskRequest):
        yield from self.events


def test_private_reasoning_credentials_and_bound_parameters_never_reach_the_wire(
    tmp_path: Any, compose: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "transport-super-secret"
    monkeypatch.setenv("QANERIS_TEST_TOKEN", secret)
    events = [
        AskEvent(
            event_type="accepted",
            sequence=1,
            correlation_id="correlation",
            payload={
                "reasoning_content": "private chain of thought",
                "chain_of_thought": "hidden deliberation",
                "password": "plain-password",
                "access_token": "plain-token",
                "parameters": ["bound-sensitive-value"],
                "message": f"provider rejected {secret}; api_key=standalone-secret-value",
            },
        ),
        AskEvent(
            event_type="done",
            sequence=2,
            correlation_id="correlation",
            payload={"status": "completed"},
        ),
    ]
    client = compose(StubService(events))

    body = sse_body(client.post(ENDPOINT, json={"question": QUESTION}))

    for leaked in (
        secret,
        "private chain of thought",
        "hidden deliberation",
        "plain-password",
        "plain-token",
        "bound-sensitive-value",
        "standalone-secret-value",
    ):
        assert leaked not in body
    assert "reasoning_content" not in body
    assert "chain_of_thought" not in body


def test_the_transport_does_not_reimplement_redaction() -> None:
    """The security boundary is ``AskEvent``: the transport must not build a second one."""
    module_source = inspect.getsource(transport)
    assert "SecretRedactor" not in module_source
    assert "redact" not in module_source


def test_a_stream_that_breaks_outside_the_contract_ends_the_response(
    tmp_path: Any, compose: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """No event is fabricated to cover a contract violation, and the client is told nothing."""

    class BrokenService:
        def ask_stream(self, request: AskRequest):
            yield AskEvent(
                event_type="accepted",
                sequence=1,
                correlation_id="correlation",
                payload={"stage": "accepted"},
            )
            raise RuntimeError(f"provider stream died with {SECRET_TOKEN}")

    client = compose(BrokenService())

    with caplog.at_level(logging.WARNING, logger=transport.__name__):
        response = client.post(ENDPOINT, json={"question": QUESTION})

    body = sse_body(response)
    assert response.status_code == 200
    assert [name for name, _ in parse_sse(body)] == ["accepted"]
    assert "done" not in body and "error" not in body
    assert "Traceback" not in body
    assert SECRET_TOKEN not in body
    # The server side gets one safe line: the exception type, never its message.
    assert "outside the event contract" in caplog.text
    assert "RuntimeError" in caplog.text
    assert SECRET_TOKEN not in caplog.text


# --------------------------------------------------------------------------------------------
# client disconnect and off-loop execution
# --------------------------------------------------------------------------------------------


class DisconnectingRequest:
    """A request whose connection is reported closed after ``after`` frames were written."""

    def __init__(self, after: int):
        self.after = after
        self.checks = 0

    async def is_disconnected(self) -> bool:
        self.checks += 1
        return self.checks > self.after


class CountingService:
    """A service double that reports how far the transport advanced its stream."""

    def __init__(self, events: list[AskEvent]):
        self.events = events
        self.consumed = 0

    def ask_stream(self, request: AskRequest):
        for event in self.events:
            self.consumed += 1
            yield event


async def drain(generator: Any) -> list[str]:
    return [frame async for frame in generator]


def contract_events(count: int) -> list[AskEvent]:
    return [
        AskEvent(
            event_type="accepted" if index == 1 else "intent_ready",
            sequence=index,
            correlation_id="correlation",
            payload={"stage": "accepted" if index == 1 else "intent"},
        )
        for index in range(1, count + 1)
    ]


def test_a_disconnected_client_stops_the_stream_without_a_fabricated_event() -> None:
    service = CountingService(contract_events(3))
    request = DisconnectingRequest(after=1)

    frames = asyncio.run(drain(stream(request, service)))

    assert len(frames) == 1
    assert json.loads(parse_sse(frames[0])[0][1])["event_type"] == "accepted"
    # The pipeline was advanced exactly once: the transport stopped instead of writing into a
    # closed connection, and no terminal event was invented.
    assert service.consumed == 1
    assert request.checks == 2
    assert "done" not in "".join(frames)


def stream(request: DisconnectingRequest, service: Any) -> Any:
    """Drive the transport with a request double standing in for the HTTP connection."""
    return transport.stream_ask_events(service, AskRequest(question=QUESTION), request)


def test_a_connected_client_receives_every_event() -> None:
    service = CountingService(contract_events(3))

    frames = asyncio.run(drain(stream(DisconnectingRequest(after=99), service)))

    assert len(frames) == 3
    assert service.consumed == 3
    assert [json.loads(data)["sequence"] for _, data in parse_sse("".join(frames))] == [1, 2, 3]


def test_the_blocking_pipeline_runs_off_the_event_loop() -> None:
    """The synchronous iterator is advanced in a worker thread, never on the event loop."""
    loop_threads: list[threading.Thread] = []
    pull_threads: list[threading.Thread] = []

    class ThreadRecordingService:
        def ask_stream(self, request: AskRequest):
            pull_threads.append(threading.current_thread())
            yield AskEvent(
                event_type="accepted",
                sequence=1,
                correlation_id="correlation",
                payload={"stage": "accepted"},
            )

    async def run() -> list[str]:
        loop_threads.append(threading.current_thread())
        return await drain(stream(DisconnectingRequest(after=99), ThreadRecordingService()))

    frames = asyncio.run(run())

    assert frames
    assert pull_threads
    assert loop_threads and all(item is not loop_threads[0] for item in pull_threads)


# --------------------------------------------------------------------------------------------
# no regression on the neighbouring surfaces
# --------------------------------------------------------------------------------------------


def test_the_synchronous_ask_endpoint_is_unchanged(tmp_path: Any, compose: Any) -> None:
    client, _, _ = running_client(tmp_path, compose)

    response = client.post(SYNC_ENDPOINT, json=ASK_BODY)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    document = response.json()
    assert AskResponse.model_validate(document).status is AskStatus.COMPLETED
    assert document["result"]["row_count"] == 1
    assert document["evidence"]["display_command"].startswith("SELECT COUNT(*)")
    # The synchronous surface still answers in one document; it did not become a stream.
    assert "text/event-stream" not in response.headers["content-type"]


def test_the_synchronous_endpoint_still_reports_product_errors_as_http_errors(
    tmp_path: Any, compose: Any
) -> None:
    service, _ = streaming_service(tmp_path)
    service.retrieve_semantics = Mock(side_effect=QanerisError("graph is not configured"))
    client = compose(service)

    response = client.post(SYNC_ENDPOINT, json={"question": QUESTION})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "qaneris_error"


def test_the_excel_upload_endpoint_is_unchanged(tmp_path: Any) -> None:
    client = TestClient(create_app(database_path=str(tmp_path / "catalog.db")))

    response = client.post(
        EXCEL_ENDPOINT, files={"file": ("notes.txt", b"not a workbook", "text/plain")}
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "EXCEL_INVALID_EXTENSION"


def test_both_ask_routes_are_registered(tmp_path: Any) -> None:
    app = create_app(database_path=str(tmp_path / "catalog.db"))
    routes = {
        route.path: sorted(route.methods)
        for route in app.routes
        if getattr(route, "methods", None) is not None
    }

    assert "POST" in routes[SYNC_ENDPOINT]
    assert "POST" in routes[ENDPOINT]


# --------------------------------------------------------------------------------------------
# boundary hygiene
# --------------------------------------------------------------------------------------------


def test_the_transport_never_reaches_past_the_event_contract() -> None:
    module_source = inspect.getsource(transport)
    for name in (
        "IntentParser",
        "SemanticRetriever",
        "Grounder",
        "Planner(",
        "Compiler",
        "Executor",
        "create_adapter",
        "GraphStore",
        "neo4j",
        "QanerisService(",
    ):
        assert name not in module_source, name
    assert "ask_stream" in module_source
    assert "AskEvent" in module_source
    assert "model_dump_json" in module_source


def test_the_route_carries_events_and_does_not_rebuild_them() -> None:
    app_source = inspect.getsource(create_app)
    route = app_source.split('@app.post("/api/ask/stream")', 1)[1].split("@app.", 1)[0]
    for name in ("IntentParser", "Planner(", "Compiler", "Executor", "Grounded"):
        assert name not in route, name
    assert "stream_ask_events" in route
    assert "SSE_HEADERS" in route
