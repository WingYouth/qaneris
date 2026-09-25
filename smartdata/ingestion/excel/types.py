"""Pure cell classification, type widening and SQLite canonicalisation.

Nothing in this module reads a file, opens a workbook or calls a model: the caller passes the raw
Python value plus the cell's stored number format. That keeps the whole type system deterministic
and unit-testable without a spreadsheet, which is the point of the Roadshow v1 contract — the Excel
file's own stored type is the only source of truth, and no field name (``phone``, ``zipcode``,
``id``, ``amount``) is ever used to guess a business type.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from enum import StrEnum
from typing import Any


class CellKind(StrEnum):
    """What a single stored cell value is, before column-level widening."""

    NULL = "NULL"
    BOOLEAN = "BOOLEAN"
    INTEGER = "INTEGER"
    REAL = "REAL"
    DATE = "DATE"
    DATETIME = "DATETIME"
    TIME = "TIME"
    TEXT = "TEXT"


class ColumnType(StrEnum):
    """The declared SQLite column type a whole column infers to."""

    BOOLEAN = "BOOLEAN"
    INTEGER = "INTEGER"
    REAL = "REAL"
    DATE = "DATE"
    DATETIME = "DATETIME"
    TEXT = "TEXT"


class UnsupportedCellValue(ValueError):
    """A stored cell holds something Roadshow v1 cannot represent as a scalar column value."""


# A time-only value (``hh:mm``, ``hh:mm:ss``, or an elapsed-time format) makes a date a datetime.
_ESCAPED_LITERAL = re.compile(r"\\.")
_QUOTED_LITERAL = re.compile(r'"[^"]*"')
_BRACKETED_SECTION = re.compile(r"\[[^\]]*\]")
_ELAPSED_TIME_SECTION = re.compile(r"\[[hms]+\]", re.IGNORECASE)
_TIME_TOKEN = re.compile(r"[hs]", re.IGNORECASE)

_MIDNIGHT = dt.time(0, 0, 0)

# Widening candidates: a kind's own column type, or ``None`` for kinds that do not decide a type.
_KIND_TO_TYPE: dict[CellKind, ColumnType | None] = {
    CellKind.NULL: None,
    CellKind.BOOLEAN: ColumnType.BOOLEAN,
    CellKind.INTEGER: ColumnType.INTEGER,
    CellKind.REAL: ColumnType.REAL,
    CellKind.DATE: ColumnType.DATE,
    CellKind.DATETIME: ColumnType.DATETIME,
    # Roadshow v1 has no TIME column type: a time-only value is stored as text.
    CellKind.TIME: ColumnType.TEXT,
    CellKind.TEXT: ColumnType.TEXT,
}


def format_has_time(number_format: str | None) -> bool:
    """Whether a stored number format carries a time-of-day component.

    Literal sections (quoted text, elapsed-time sections, escaped characters) are removed first so
    ``yyyy\\-mm\\-dd`` stays a date while ``[h]:mm:ss`` and ``hh:mm`` do not.
    """
    if not number_format:
        return False
    if _ELAPSED_TIME_SECTION.search(number_format):
        return True
    stripped = _BRACKETED_SECTION.sub(
        "", _ESCAPED_LITERAL.sub("", _QUOTED_LITERAL.sub("", number_format))
    )
    if _TIME_TOKEN.search(stripped):
        return True
    return "am/pm" in stripped.lower()


def classify_cell(value: Any, number_format: str | None = None) -> CellKind:
    """Classify one stored cell value, or fail for a value v1 cannot represent."""
    if value is None:
        return CellKind.NULL
    # ``bool`` must be tested before ``int``: in Python ``bool`` is an ``int`` subclass.
    if isinstance(value, bool):
        return CellKind.BOOLEAN
    if isinstance(value, int):
        return CellKind.INTEGER
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise UnsupportedCellValue("NaN / Infinity is not a valid REAL cell value")
        return CellKind.REAL
    # ``datetime`` must be tested before ``date``: in Python ``datetime`` is a ``date`` subclass.
    if isinstance(value, dt.datetime):
        if value.time() == _MIDNIGHT and not format_has_time(number_format):
            return CellKind.DATE
        return CellKind.DATETIME
    if isinstance(value, dt.date):
        return CellKind.DATE
    if isinstance(value, dt.time):
        return CellKind.TIME
    if isinstance(value, str):
        return CellKind.TEXT
    raise UnsupportedCellValue(f"unsupported cell value type: {type(value).__name__}")


def widen(left: ColumnType | None, right: ColumnType | None) -> ColumnType | None:
    """Combine two candidate column types using the fixed Roadshow v1 widening table."""
    if left is None:
        return right
    if right is None:
        return left
    if left == right:
        return left
    if ColumnType.TEXT in (left, right):
        return ColumnType.TEXT
    if {left, right} == {ColumnType.INTEGER, ColumnType.REAL}:
        return ColumnType.REAL
    if {left, right} == {ColumnType.DATE, ColumnType.DATETIME}:
        return ColumnType.DATETIME
    # BOOLEAN with a number, DATE/DATETIME with a number or boolean, and anything unlisted.
    return ColumnType.TEXT


def infer_column_type(kinds: list[CellKind]) -> ColumnType:
    """Fold every non-null cell kind of one column into its declared type.

    An all-null column declares TEXT, and the caller marks it ``all_null=true`` in the manifest.
    """
    result: ColumnType | None = None
    for kind in kinds:
        result = widen(result, _KIND_TO_TYPE[kind])
    return result or ColumnType.TEXT


def as_datetime(value: dt.date | dt.datetime) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return value
    return dt.datetime.combine(value, _MIDNIGHT)


def canonical_text(value: Any, kind: CellKind | None = None) -> str:
    """The one text form of a value, used for TEXT columns and for TEXT-widened columns.

    A temporal value keeps the same representation whichever column type it lands in, so a DATE
    stored in a TEXT column reads exactly like a DATE stored in a DATE column.
    """
    resolved = classify_cell(value) if kind is None else kind
    if resolved is CellKind.NULL:
        return ""
    if resolved is CellKind.BOOLEAN:
        return "true" if value else "false"
    if resolved is CellKind.INTEGER:
        return str(int(value))
    if resolved is CellKind.REAL:
        return str(float(value))
    if resolved is CellKind.DATE:
        return as_datetime(value).date().isoformat()
    if resolved is CellKind.DATETIME:
        return as_datetime(value).isoformat()
    if resolved is CellKind.TIME:
        return value.isoformat()
    return str(value)


def coerce_cell(value: Any, number_format: str | None, column_type: ColumnType) -> Any:
    """Convert a stored cell value into the exact value written to SQLite."""
    if value is None:
        return None
    if column_type is ColumnType.BOOLEAN:
        return 1 if value else 0
    if column_type is ColumnType.INTEGER:
        return int(value)
    if column_type is ColumnType.REAL:
        return float(value)
    if column_type is ColumnType.DATE:
        return as_datetime(value).date().isoformat()
    if column_type is ColumnType.DATETIME:
        return as_datetime(value).isoformat()
    return canonical_text(value, classify_cell(value, number_format))
