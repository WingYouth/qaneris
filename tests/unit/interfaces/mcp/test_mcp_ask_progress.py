"""``ask_data``: the AskEvent stream is the progress stream (RS-MCP-01).

These tests drive the tool through a real MCP client session with a substituted service, so what is
asserted is the actual protocol behavior - the progress notifications a client receives, the tool
result it reads and the ``isError`` flag - rather than a re-implementation of it.

The rules under test:

* **1:1.** One progress notification per ``AskEvent``, in order, with the event type as the message.
* **No fabricated total.** An Ask may complete, clarify or fail, so the event count is unknowable in
  advance and ``total`` is never sent.
* **One Ask.** The terminal response is read off the stream. ``service.ask()`` is never called, so a
  question is never answered twice.
* **Clarification is a result, not an error.** It returns success with the clarification attached.
* **A failure keeps its stable code** and never carries the original exception text.
"""

from __future__ import annotations

import json
from typing import Any

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session as connected_session

from qaneris.contracts import (
    AskEvent,
    AskEventType,
    AskResponse,
    AskStatus,
    ErrorDetail,
)
from qaneris.interfaces.mcp import server


class FakeContext:
    """Records exactly what ``ask_data`` would send to a real MCP client."""

    def __init__(self) -> None:
        self.progress: list[tuple[float, float | None, str | None]] = []

    async def report_progress(
        self, progress: float, total: float | None = None, message: str | None = None
    ) -> None:
        self.progress.append((progress, total, message))


def event(sequence: int, event_type: AskEventType, payload: dict[str, Any] | None = None) -> AskEvent:
    return AskEvent(
        event_type=event_type,
        sequence=sequence,
        correlation_id="corr-1",
        payload=payload or {"stage": event_type.value},
    )


def completed_stream() -> list[AskEvent]:
    stages = [
        AskEventType.ACCEPTED,
        AskEventType.INTENT_READY,
        AskEventType.RETRIEVAL_READY,
        AskEventType.GROUNDING_READY,
        AskEventType.PLAN_READY,
        AskEventType.QUERY_READY,
        AskEventType.EXECUTION_STARTED,
        AskEventType.RESULT_READY,
        AskEventType.DONE,
    ]
    response = AskResponse(
        question="按地区看销售额", status=AskStatus.COMPLETED, answer="已完成。"
    ).model_dump(mode="json")
    return [
        event(
            index,
            stage,
            {"stage": stage.value, "response": response}
            if stage in (AskEventType.RESULT_READY,)
            else {"stage": stage.value},
        )
        for index, stage in enumerate(stages, start=1)
    ]


def clarification_stream() -> list[AskEvent]:
    response = AskResponse(
        question="销售额", status=AskStatus.CLARIFICATION_REQUIRED, answer="请明确时间范围。"
    ).model_dump(mode="json")
    return [
        event(1, AskEventType.ACCEPTED),
        event(2, AskEventType.INTENT_READY),
        event(
            3,
            AskEventType.CLARIFICATION_REQUIRED,
            {"stage": "clarification", "response": response, "status": "clarification_required"},
        ),
        event(4, AskEventType.DONE, {"stage": "done", "response": response}),
    ]


def failed_stream(code: str = "unsafe_query", message: str = "只允许只读查询") -> list[AskEvent]:
    response = AskResponse(
        question="删除订单",
        status=AskStatus.FAILED,
        answer=message,
        error=ErrorDetail(code=code, message=message),
    ).model_dump(mode="json")
    return [
        event(1, AskEventType.ACCEPTED),
        event(
            2,
            AskEventType.ERROR,
            {"stage": "error", "error": {"code": code, "message": message}, "response": response},
        ),
        event(3, AskEventType.DONE, {"stage": "done", "response": response}),
    ]


class FakeService:
    """A service whose Ask is a canned event stream, and which records every call."""

    def __init__(self, events: list[AskEvent]):
        self.events = events
        self.stream_calls = 0
        self.ask_calls = 0

    def ask_stream(self, request: Any):
        self.stream_calls += 1
        yield from self.events

    def ask(self, request: Any):
        self.ask_calls += 1
        raise AssertionError("ask_data must not run a second Ask through service.ask()")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def install(monkeypatch, events: list[AskEvent]) -> FakeService:
    replacement = FakeService(events)
    monkeypatch.setattr(server, "service", replacement)
    return replacement


@pytest.mark.anyio
async def test_progress_is_one_to_one_with_events(monkeypatch) -> None:
    fake = install(monkeypatch, completed_stream())
    context = FakeContext()

    result = await server.ask_data("按地区看销售额", ctx=context)

    assert fake.stream_calls == 1
    assert len(context.progress) == len(fake.events)
    assert [item[2] for item in context.progress] == [
        item.event_type.value for item in fake.events
    ]
    # Monotonic, ascending, and equal to the event sequence - not a re-based counter.
    assert [item[0] for item in context.progress] == [
        float(item.sequence) for item in fake.events
    ]
    assert all(
        later[0] > earlier[0]
        for earlier, later in zip(context.progress, context.progress[1:], strict=False)
    )
    # No total: the event count is not known before the Ask runs.
    assert all(item[1] is None for item in context.progress)
    assert context.progress[-1][2] == "done"
    assert json.loads(result)["status"] == "completed"


@pytest.mark.anyio
async def test_progress_message_never_carries_the_payload(monkeypatch) -> None:
    """Progress is a stage notice; the result is returned once, as the tool result."""
    install(monkeypatch, completed_stream())
    context = FakeContext()

    result = await server.ask_data("按地区看销售额", ctx=context)

    messages = [item[2] for item in context.progress]
    assert all(message in {stage.value for stage in AskEventType} for message in messages)
    assert not any("response" in message or "rows" in message for message in messages)
    # The result really is in the tool result, so nothing is lost by the terse messages.
    assert "answer" in json.loads(result)


@pytest.mark.anyio
async def test_clarification_is_a_successful_result(monkeypatch) -> None:
    install(monkeypatch, clarification_stream())
    context = FakeContext()

    result = await server.ask_data("销售额", ctx=context)

    assert context.progress[-1][2] == "done"
    payload = json.loads(result)
    assert payload["status"] == "clarification_required"
    assert payload["answer"] == "请明确时间范围。"


@pytest.mark.anyio
async def test_failure_keeps_the_stable_code_and_hides_the_original(monkeypatch) -> None:
    install(monkeypatch, failed_stream())
    context = FakeContext()

    with pytest.raises(RuntimeError) as raised:
        await server.ask_data("删除订单", ctx=context)

    assert "unsafe_query" in str(raised.value)
    # Every progress notification was delivered before the failure surfaced.
    assert [item[2] for item in context.progress] == ["accepted", "error", "done"]


@pytest.mark.anyio
async def test_unexpected_stream_failure_exposes_only_the_type(monkeypatch) -> None:
    """An exception the stream contract does not contain is reduced, not published verbatim."""

    class Exploding(FakeService):
        def ask_stream(self, request: Any):
            self.stream_calls += 1
            raise RuntimeError("driver detail: host=10.0.0.1 password=hunter2")
            yield  # pragma: no cover - makes this a generator

    monkeypatch.setattr(server, "service", Exploding([]))

    with pytest.raises(RuntimeError) as raised:
        await server.ask_data("x", ctx=FakeContext())

    assert "operation_failed" in str(raised.value)
    assert "RuntimeError" in str(raised.value)
    assert "hunter2" not in str(raised.value)


@pytest.mark.anyio
async def test_progress_arrives_before_the_ask_finishes(monkeypatch) -> None:
    """Progress must be produced *during* the Ask, not replayed after it.

    The substituted stream parks after its first event. If the tool ran the iterator to completion
    before reporting anything - or buffered the events and reported them at the end - nothing would
    be visible while it was parked, and the assertion below would see an empty list.
    """
    gate = anyio.Event()
    observed_while_parked: list[str | None] = []
    context = FakeContext()
    response = AskResponse(
        question="slow", status=AskStatus.COMPLETED, answer="已完成。"
    ).model_dump(mode="json")

    class SlowService(FakeService):
        def ask_stream(self, request: Any):
            self.stream_calls += 1
            yield event(1, AskEventType.ACCEPTED, {"stage": "accepted"})
            anyio.from_thread.run(gate.wait)
            yield event(2, AskEventType.RESULT_READY, {"stage": "result", "response": response})
            yield event(3, AskEventType.DONE, {"stage": "done", "response": response})

    monkeypatch.setattr(server, "service", SlowService([]))

    async def release_later() -> None:
        # Sample the notifications while the Ask is still parked mid-stream.
        await anyio.sleep(0.05)
        observed_while_parked.extend(item[2] for item in context.progress)
        gate.set()

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(release_later)
        await server.ask_data("slow", ctx=context)

    assert observed_while_parked == ["accepted"]
    assert [item[2] for item in context.progress] == ["accepted", "result_ready", "done"]


@pytest.mark.anyio
async def test_ask_runs_once_and_never_calls_service_ask(monkeypatch) -> None:
    fake = install(monkeypatch, completed_stream())

    await server.ask_data("按地区看销售额", ctx=FakeContext())

    assert fake.stream_calls == 1
    assert fake.ask_calls == 0


@pytest.mark.anyio
async def test_works_without_a_progress_callback(monkeypatch) -> None:
    """A client that cannot receive progress still gets the same product result."""
    fake = install(monkeypatch, completed_stream())

    result = await server.ask_data("按地区看销售额", ctx=None)

    assert fake.stream_calls == 1
    assert json.loads(result)["status"] == "completed"


@pytest.mark.anyio
async def test_progress_over_the_real_protocol(monkeypatch, tmp_path) -> None:
    """The same behavior, observed by a real MCP client through a real session."""
    install(monkeypatch, completed_stream())
    observed: list[tuple[float, float | None, str | None]] = []

    async def callback(progress: float, total: float | None, message: str | None) -> None:
        observed.append((progress, total, message))

    async with connected_session(server.mcp._mcp_server) as client:
        result = await client.call_tool(
            "ask_data", {"question": "按地区看销售额"}, progress_callback=callback
        )

    assert not result.isError
    assert [item[2] for item in observed] == [
        item.event_type.value for item in completed_stream()
    ]
    assert observed[0][2] == "accepted"
    assert observed[-1][2] == "done"
    assert "result_ready" in [item[2] for item in observed]
    assert all(item[1] is None for item in observed)


@pytest.mark.anyio
async def test_failure_over_the_real_protocol(monkeypatch) -> None:
    install(monkeypatch, failed_stream())

    async with connected_session(server.mcp._mcp_server) as client:
        result = await client.call_tool("ask_data", {"question": "删除订单"})

    assert result.isError
    assert "unsafe_query" in result.content[0].text
