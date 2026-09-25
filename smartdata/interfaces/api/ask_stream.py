"""HTTP SSE transport for the unified Ask event stream (RS-STREAM-01B).

This module owns the transport and nothing else. It knows no intent, no retrieval, no grounding, no
planner, no compiler and no executor: it asks ``SmartDataService.ask_stream()`` for events and puts
them on the wire. Exactly three concerns live here, and each one exists because a browser cannot
have it any other way:

1. **Framing.** One ``AskEvent`` becomes one Server-Sent Event. The ``data`` field is the event's
   own JSON - ``AskEvent`` serialises itself - so the transport cannot drift from the event
   contract, cannot add a field of its own, and cannot rewrite, renumber or re-wrap a payload. The
   SSE event name is the contract's own ``event_type``, so a client can dispatch on it directly.
2. **Off-loop execution.** ``ask_stream()`` is a synchronous iterator that talks to a model and to
   databases, so it is advanced one step per frame in a worker thread. The event loop stays free
   while a stage runs, and the Application Service keeps its existing synchronous shape.
3. **Honest termination.** The transport never invents a stage. A clarification, a product failure
   and a completed run are all carried as whatever events the service produced, and a stream that
   ends outside the contract ends the response instead of being covered by a fabricated
   ``error`` / ``done`` pair.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator

from fastapi import Request
from starlette.concurrency import run_in_threadpool

from smartdata.application.service import SmartDataService
from smartdata.contracts import AskEvent, AskRequest

#: The media type named by the Server-Sent Events specification. Every frame is ``text/event-stream``.
SSE_MEDIA_TYPE = "text/event-stream"

#: SSE response headers.
#:
#: ``Content-Type`` is spelled exactly as the specification names it. Starlette would otherwise
#: append ``; charset=utf-8`` (its rule for every ``text/*`` media type), but SSE consumers always
#: decode UTF-8 by specification, and the payload is UTF-8 JSON either way - so the parameter adds
#: nothing and the header is kept literal.
#:
#: ``no-cache`` stops a browser or an intermediary from replaying a progress stream it has already
#: seen. No proxy-specific header is set: the deployment runs uvicorn directly (see
#: ``docker-compose.yml``), so there is no buffering proxy to configure.
SSE_HEADERS = {"Content-Type": SSE_MEDIA_TYPE, "Cache-Control": "no-cache"}

logger = logging.getLogger(__name__)


def sse_frame(event: AskEvent) -> str:
    """Encode one contract event as one SSE frame.

    ``AskEvent.model_dump_json()`` is the event's own serialization, so the frame carries no second
    schema and no transport-only field. ``data`` is always a single line: JSON escapes every
    newline inside the document, which is what the SSE field framing requires.
    """
    return f"event: {event.event_type.value}\ndata: {event.model_dump_json()}\n\n"


def _next_frame(events: Iterator[AskEvent]) -> str | None:
    """Pull one event and encode it; ``None`` means the stream is finished.

    ``next(events, None)`` rather than a bare ``next`` because this runs in a worker thread, where a
    raised ``StopIteration`` could not be told apart from an empty result.
    """
    event = next(events, None)
    return None if event is None else sse_frame(event)


async def stream_ask_events(
    service: SmartDataService, request: AskRequest, http_request: Request
) -> AsyncIterator[str]:
    """Yield the Ask event stream as SSE frames, in the order the Application Service produced them.

    The transport is deliberately transparent: it does not reorder, renumber, re-wrap or drop an
    event, does not synthesise a stage the pipeline never reached, and does not translate a product
    outcome. A clarification and a failure are both complete, successful responses here - the stream
    simply ends with ``done``, exactly as the service sent it.

    A client that has gone away stops the stream: the connection is checked between frames and the
    generator returns, so nothing is written into a closed connection and no event is invented to
    cover the gap.
    """
    events = service.ask_stream(request)
    while True:
        if await http_request.is_disconnected():
            # The client closed the connection. Stop advancing the pipeline instead of writing into
            # a dead socket; the iterator is left to the runtime to close and no frame is emitted.
            return
        try:
            frame = await run_in_threadpool(_next_frame, events)
        except Exception as error:  # noqa: BLE001 - the transport boundary
            # ``ask_stream()`` reports a pipeline failure as ``error`` followed by ``done``, so
            # reaching this handler means the stream broke *outside* that contract. The response is
            # terminated and no event is fabricated to cover the gap. Only the exception type is
            # logged - a message can carry a provider credential - and the client is told nothing,
            # because a response that has already started cannot become an error response.
            logger.warning("ask stream ended outside the event contract: %s", type(error).__name__)
            return
        if frame is None:
            return
        yield frame
