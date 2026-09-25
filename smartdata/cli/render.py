"""Display-only rendering helpers shared by the product CLI commands.

These helpers format values for a human terminal. They never decide what the product means: the
Application Service and the contracts own every product rule, and nothing here reads a catalog, a
graph or a database. They also never rewrite the data they are handed - a truncated cell is a
display limit, not a value change.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from smartdata.common.errors import SmartDataError
from smartdata.common.redaction import SecretRedactor

#: Long business values are truncated for display only; the data itself is never rewritten.
CELL_DISPLAY_LIMIT = 60


def cell(value: Any, *, limit: int = CELL_DISPLAY_LIMIT) -> str:
    """Render one value as a single display cell."""
    text = "null" if value is None else str(value)
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def render_table(columns: Sequence[str], rows: Sequence[dict[str, Any]]) -> list[str]:
    """Render a plain standard-library table. Display only; values are never altered."""
    if not columns:
        return []
    cells = [[cell(row.get(column)) for column in columns] for row in rows]
    widths = [len(column) for column in columns]
    for line in cells:
        for index, text in enumerate(line):
            widths[index] = max(widths[index], len(text))
    header = "  ".join(column.ljust(widths[index]) for index, column in enumerate(columns))
    separator = "  ".join("-" * width for width in widths)
    lines = [header, separator]
    lines.extend(
        "  ".join(text.ljust(widths[index]) for index, text in enumerate(line)) for line in cells
    )
    return lines


def render_fields(fields: Sequence[tuple[str, Any]]) -> list[str]:
    """Render ``Label: value`` lines for a single record."""
    return [f"{label}: {cell(value)}" for label, value in fields]


def operation_error_detail(error: Exception) -> dict[str, Any]:
    """Return the publishable ``(type, code, message)`` of a failed operation.

    A ``SmartDataError`` keeps its stable machine code and its redacted message. Any other exception
    exposes only its type, because an arbitrary message could carry a provider credential or a
    bound query parameter value.
    """
    if isinstance(error, SmartDataError):
        return {
            "type": type(error).__name__,
            "code": error.code,
            "message": SecretRedactor.from_environment().text(error.message),
        }
    return {"type": type(error).__name__, "code": None, "message": type(error).__name__}
