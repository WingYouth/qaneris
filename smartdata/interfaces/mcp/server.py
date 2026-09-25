"""MCP server entry point.

Every tool here is a thin projection of one ``SmartDataService`` public method. The MCP boundary
owns three things and nothing else:

* **Argument shaping** - a tool declares typed Pydantic inputs (``SecureDatasourceCreate`` and
  friends) instead of accepting an opaque JSON string, so the MCP SDK generates a real schema and
  an agent never has to guess at a connection format.
* **Progress translation** - ``ask_data`` maps the ``AskEvent`` stream 1:1 onto MCP progress
  notifications. There is no second event vocabulary: the event sequence *is* the progress.
* **Error translation** - a ``SmartDataError`` keeps its stable code and redacted message; any
  other exception is reduced to its type.

It deliberately does **not** read a catalog, resolve a secret, materialize TLS, open an adapter or
call a model. Those belong to the Application Service, and a tool that reached past it would be a
second, unaudited path to the same data. ``tests/unit/interfaces/mcp/test_mcp_boundary.py`` guards
that mechanically.

Credentials are never accepted here. A tool may take a ``SecretReference`` - a ``provider`` plus an
``identifier`` - and nothing that could carry a secret *value*. A password, token, certificate or
private key must be created through the CLI or the HTTP API first, and this server then refers to
it by name.
"""

from __future__ import annotations

import functools
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

import anyio
from mcp.server.fastmcp import Context, FastMCP

from smartdata.application.service import SmartDataService
from smartdata.common.errors import SmartDataError
from smartdata.common.redaction import SecretRedactor
from smartdata.contracts import (
    AskEvent,
    AskEventType,
    AskRequest,
    DatasourceKind,
)
from smartdata.contracts.connection import (
    ConnectionProfile,
    SecureDatasourceCreate,
    SecureDatasourceTest,
    SecureDatasourceUpdate,
)

mcp = FastMCP("SmartData")
service = SmartDataService.from_environment()

#: Events in flight between the worker thread and the event loop. Zero is a rendezvous, and that is
#: deliberate: it makes the producer wait at every stage until the loop has taken the previous event
#: and reported it. A buffered channel would let the thread run the whole Ask to the end while the
#: notifications were still queued, which would deliver real events as one fake-looking burst.
_ASK_EVENT_BUFFER = 0

_ToolResult = TypeVar("_ToolResult")


class ToolFailure(RuntimeError):
    """An exception whose text is already safe to publish to an MCP client.

    The error boundary reduces an unknown exception to this type, and passes an existing instance
    straight through. That distinction lets a tool that has *already* built a safe message - which
    ``ask_data`` must, because the stable code lives on the ``error`` AskEvent rather than on a
    Python exception - opt out of being reduced twice and losing that code.
    """


def _innermost(error: BaseException) -> BaseException:
    """Unwrap a task-group ``ExceptionGroup`` down to the exception that actually failed.

    AnyIO runs the stream driver in a task group, and a task group re-raises whatever its children
    raised wrapped in an ``ExceptionGroup``. Publishing the wrapper would report the type as
    ``ExceptionGroup`` for every failure - true, useless, and it hides the type the operator needs.
    Only groups are unwrapped: anything else is returned as-is, so a cancellation is never mistaken
    for a product failure and swallowed.
    """
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    return error


def _publishable_error(error: Exception) -> Exception:
    """Reduce any exception to something safe to hand an MCP client.

    A ``SmartDataError`` already carries a stable machine code and a message the application
    sanitized, so both survive. Anything else is an unexpected failure whose text could quote a
    driver exception, a host or a bound parameter value, so only the type name is published.
    """
    if isinstance(error, ToolFailure):
        return error
    inner = _innermost(error)
    if isinstance(inner, ToolFailure):
        return inner
    if isinstance(inner, SmartDataError):
        message = SecretRedactor.from_environment().text(inner.message)
        return ToolFailure(f"{inner.code}: {message}")
    return ToolFailure(f"operation_failed: {type(inner).__name__}")


def _guarded(
    function: Callable[..., Awaitable[_ToolResult]],
) -> Callable[..., Awaitable[_ToolResult]]:
    """Apply the error boundary to one async tool.

    ``functools.wraps`` is what keeps this transparent to the MCP SDK: the SDK reads the wrapped
    signature to build the tool schema and to decide where to inject ``Context``.
    """

    @functools.wraps(function)
    async def wrapper(*args: Any, **kwargs: Any) -> _ToolResult:
        try:
            return await function(*args, **kwargs)
        except Exception as error:  # noqa: BLE001 - this is the product error boundary
            raise _publishable_error(error) from None

    return wrapper


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _dump(items: Any) -> list[dict[str, Any]]:
    return [item.model_dump(mode="json") for item in items]


# -- discovery ---------------------------------------------------------------------------------


@mcp.tool()
@_guarded
async def list_adapters() -> str:
    """List database kinds and drivers supported by this SmartData installation."""
    return _json(service.list_supported_adapters())


@mcp.tool()
@_guarded
async def get_schema_context(workspace_id: str = "default") -> str:
    """Return normalized structures, relations, bounded samples, and mapping candidates."""
    return _json(service.get_schema_context(workspace_id))


@mcp.tool()
@_guarded
async def summarize_schema(workspace_id: str = "default") -> str:
    """Summarize the workspace schema with the configured model, or a deterministic fallback."""
    return service.summarize_schema(workspace_id)


@mcp.tool()
@_guarded
async def search_dataset(query: str, workspace_id: str = "default") -> str:
    """Search catalog datasets and fields by name."""
    return _json(_dump(service.search_datasets(query, workspace_id)))


@mcp.tool()
@_guarded
async def list_relations(workspace_id: str = "default", datasource_id: str | None = None) -> str:
    """List structural relations discovered inside data sources."""
    return _json(_dump(service.list_relations(workspace_id, datasource_id)))


@mcp.tool()
@_guarded
async def list_mappings(workspace_id: str = "default", entity: str | None = None) -> str:
    """List physical-to-canonical field mappings in a workspace."""
    return _json(_dump(service.list_mappings(workspace_id, entity)))


@mcp.tool()
@_guarded
async def list_governance_suggestions(workspace_id: str = "default") -> str:
    """List governance suggestions awaiting human review."""
    return _json(_dump(service.list_suggestions(workspace_id)))


# -- datasource lifecycle ----------------------------------------------------------------------


@mcp.tool()
@_guarded
async def list_datasources(workspace_id: str = "default") -> str:
    """List configured data sources without exposing credentials."""
    return _json(_dump(service.list_datasources(workspace_id)))


@mcp.tool()
@_guarded
async def inspect_datasource(datasource_id: str) -> str:
    """Show one datasource's public fields and its current scan facts."""
    return service.inspect_datasource(datasource_id).model_dump_json()


@mcp.tool()
@_guarded
async def test_secure_datasource(kind: DatasourceKind, connection_profile: ConnectionProfile) -> str:
    """Test a candidate connection without saving, scanning or publishing anything."""
    result = service.test_secure_datasource(
        SecureDatasourceTest(kind=kind, connection_profile=connection_profile)
    )
    return result.model_dump_json()


@mcp.tool()
@_guarded
async def create_secure_datasource(
    name: str,
    kind: DatasourceKind,
    connection_profile: ConnectionProfile,
    workspace_id: str = "default",
) -> str:
    """Save a tested connection as a datasource. The datasource stays ``created`` until scanned."""
    datasource = service.create_secure_datasource(
        SecureDatasourceCreate(
            name=name,
            kind=kind,
            connection_profile=connection_profile,
            workspace_id=workspace_id,
        )
    )
    return datasource.model_dump_json()


@mcp.tool()
@_guarded
async def update_secure_datasource(
    datasource_id: str, connection_profile: ConnectionProfile
) -> str:
    """Switch a datasource onto a new connection. Scan again afterwards to reach READY."""
    datasource = service.update_secure_datasource(
        datasource_id, SecureDatasourceUpdate(connection_profile=connection_profile)
    )
    return datasource.model_dump_json()


@mcp.tool()
@_guarded
async def delete_datasource(datasource_id: str) -> str:
    """Delete a datasource together with its published graph and unreferenced secrets."""
    service.delete_datasource(datasource_id)
    return _json({"status": "deleted", "datasource_id": datasource_id})


@mcp.tool()
@_guarded
async def scan_datasource(datasource_id: str) -> str:
    """Scan a data source and update its datasets and fields in the catalog."""
    return _json(_dump(service.scan_datasource(datasource_id)))


@mcp.tool()
@_guarded
async def register_sqlite(name: str, path: str, workspace_id: str = "default") -> str:
    """Register an existing local SQLite database.

    A convenience wrapper over the secure datasource contract for the one driver that needs no
    credentials and no TLS. It is deliberately not a shortcut past it: the same
    ``SecureDatasourceCreate`` is built and the same create path runs, so the result is identical
    to calling ``create_secure_datasource`` - including staying ``created`` until ``scan_datasource``
    is called.
    """
    datasource = service.create_secure_datasource(
        SecureDatasourceCreate(
            name=name,
            kind=DatasourceKind.RELATIONAL,
            connection_profile=ConnectionProfile(
                driver="sqlite",
                endpoint={"path": path},
                authentication={"method": "none"},
                tls={"enabled": False},
            ),
            workspace_id=workspace_id,
        )
    )
    return datasource.model_dump_json()


# -- ask ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _AskStreamFailure:
    """An unexpected failure raised by the stream driver, carried back to the event loop."""

    error: Exception


def _forward(send_stream: anyio.abc.ObjectSendStream[Any], item: Any) -> bool:
    """Hand one item to the event loop; ``False`` once the channel is unusable.

    The channel is unusable when the consumer stopped reading - the client disconnected, or the
    caller left the ``async for`` early - or when the loop itself is being torn down. Neither is an
    error worth reporting: both mean "nobody is waiting for this Ask any more", and the only useful
    response is to stop.
    """
    try:
        anyio.from_thread.run(send_stream.send, item)
    except (anyio.BrokenResourceError, anyio.ClosedResourceError, RuntimeError):
        return False
    return True


def _drive_ask_stream(
    request: AskRequest,
    send_stream: anyio.abc.ObjectSendStream[Any],
) -> None:
    """Run the synchronous ``ask_stream`` generator on a worker thread and forward its events.

    ``SmartDataService.ask_stream()`` is a plain ``Iterator``, so it must not be driven on the event
    loop: a whole Ask would run synchronously and every progress notification would arrive at the
    end, which is exactly the fake progress this design exists to avoid. Pulling events on a worker
    thread and handing each one back to the loop as it is produced keeps the loop responsive and the
    progress live.

    A product failure is not special-cased here. ``ask_stream`` already represents one as an ``error``
    event followed by ``done``, so the error travels back as a normal event and the caller reads it
    off the stream like any other outcome. This wrapper only catches what the stream contract does
    not promise to contain.
    """

    try:
        for event in service.ask_stream(request):
            if not _forward(send_stream, event):
                return
    except Exception as error:  # noqa: BLE001 - forwarded, not handled, on this thread
        _forward(send_stream, _AskStreamFailure(error))
    finally:
        try:
            anyio.from_thread.run(send_stream.aclose)
        except (anyio.BrokenResourceError, anyio.ClosedResourceError, RuntimeError):
            # The consumer is already gone, so the stream is closed on its side too.
            pass


async def _ask_events(request: AskRequest) -> AsyncIterator[AskEvent]:
    """Yield ``AskEvent`` objects as the worker thread produces them."""
    send_stream, receive_stream = anyio.create_memory_object_stream[Any](_ASK_EVENT_BUFFER)

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(
            anyio.to_thread.run_sync, lambda: _drive_ask_stream(request, send_stream)
        )
        async with receive_stream:
            async for item in receive_stream:
                if isinstance(item, _AskStreamFailure):
                    raise item.error
                yield item


@mcp.tool()
@_guarded
async def ask_data(
    question: str,
    workspace_id: str = "default",
    datasource_id: str | None = None,
    sql: str | None = None,
    max_rows: int = 200,
    ctx: Context | None = None,
) -> str:
    """Answer a business data question; optional SQL must be read-only.

    Progress is the ``AskEvent`` stream itself - one MCP progress notification per event, in order -
    so a client sees the real stages of this Ask (``accepted``, ``intent_ready``, ... ``done``)
    rather than a timer. No ``total`` is reported: an Ask may complete, ask for clarification or
    fail, so the event count is not known in advance and inventing one would be a lie.

    A clarification is a normal product outcome, not an error. Only a failed Ask is reported as an
    MCP tool error, carrying the stable SmartData code the service already published.
    """
    request = AskRequest(
        question=question,
        workspace_id=workspace_id,
        datasource_id=datasource_id,
        sql=sql,
        max_rows=max_rows,
    )

    response: dict[str, Any] | None = None
    failure: tuple[str, str] | None = None

    async for event in _ask_events(request):
        if ctx is not None:
            # The message is the event type alone. The payload - which can hold a result, an
            # evidence document or a clarification - is returned once, as the tool result, so it is
            # not repeated across every progress notification.
            await ctx.report_progress(
                progress=float(event.sequence),
                message=event.event_type.value,
            )
        if event.event_type is AskEventType.ERROR:
            # The code and message below were projected and redacted by the service before they
            # entered the payload. Re-raising the original exception is not an option here - the
            # stream swallowed it - so the published pair is what carries the stable code out.
            error = event.payload.get("error") or {}
            failure = (
                str(error.get("code", "internal_error")),
                str(error.get("message", "问数执行失败。")),
            )
        elif event.event_type in (
            AskEventType.RESULT_READY,
            AskEventType.CLARIFICATION_REQUIRED,
        ):
            candidate = event.payload.get("response")
            if isinstance(candidate, dict):
                response = candidate

    if failure is not None:
        raise ToolFailure(f"{failure[0]}: {failure[1]}")
    if response is None:  # pragma: no cover - guarded by the orchestration contract
        raise ToolFailure("operation_failed: Ask ended without a terminal response")
    return json.dumps(response, ensure_ascii=False)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
