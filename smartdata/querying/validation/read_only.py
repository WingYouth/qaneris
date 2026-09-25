r"""Read-only SQL safety validation.

Stripping rules (applied left-to-right, depth-aware for ``--`` and ``/* */``):
- a single-quoted string literal ``'...'`` (with embedded ``''`` doubled)
- a double-quoted identifier ``"..."`` (with embedded ``""`` doubled)
- a back-quoted identifier ``\`...\``` (MySQL style)
- a square-bracket identifier ``[...]`` (SQL Server / Access style)
- a ``-- ...`` line comment up to the next newline
- a ``/* ... */`` block comment (nested allowed)
"""

from __future__ import annotations

import re

from smartdata.common.errors import QuerySafetyError


class UnsafeQueryError(QuerySafetyError):
    pass


# Top-level forbidden operations (the leading token of the statement).
# These are only matched AFTER comments and quoted regions are stripped.
_FORBIDDEN_LEAD = re.compile(
    r"^\s*(insert|update|delete|drop|alter|create|attach|detach|pragma|vacuum|"
    r"replace|reindex|analyze)\b",
    re.IGNORECASE,
)

# Whole-statement forbidden keywords that can be detected only after stripping
# quotes / comments. A write can also appear after a SELECT (e.g. SELECT ...
# INTO); Phase 1 keeps that out by rejecting any of these tokens at all.
_FORBIDDEN_ANY = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|vacuum|"
    r"replace|reindex|analyze)\b",
    re.IGNORECASE,
)


def _strip_for_safety(sql: str) -> str:
    """Return ``sql`` with quoted regions / comments replaced by spaces.

    Quoted regions and comments cannot influence the safety decision because the
    driver treats them as opaque text. A "delete" inside ``"delete"`` is an
    identifier, not a keyword.
    """
    out = list(sql)
    index = 0
    while index < len(sql):
        char = sql[index]
        following = sql[index + 1] if index + 1 < len(sql) else ""

        # Block comment (with one level of nesting; SQLite matches PG here).
        if char == "/" and following == "*":
            end = index + 2
            depth = 1
            while end < len(sql) and depth > 0:
                if end + 1 < len(sql) and sql[end] == "/" and sql[end + 1] == "*":
                    depth += 1
                    end += 2
                    continue
                if end + 1 < len(sql) and sql[end] == "*" and sql[end + 1] == "/":
                    depth -= 1
                    end += 2
                    continue
                end += 1
            for position in range(index, min(end, len(sql))):
                if out[position] != "\n":
                    out[position] = " "
            index = end
            continue

        # Line comment.
        if char == "-" and following == "-":
            newline = sql.find("\n", index + 2)
            end = len(sql) if newline < 0 else newline + 1
            for position in range(index, end):
                if out[position] != "\n":
                    out[position] = " "
            index = end
            continue

        # Single-quoted string literal.
        if char == "'":
            end = index + 1
            while end < len(sql):
                if sql[end] == "'":
                    if end + 1 < len(sql) and sql[end + 1] == "'":
                        end += 2
                        continue
                    end += 1
                    break
                end += 1
            else:  # unterminated string; treat as end-of-input.
                end = len(sql)
            for position in range(index, end):
                if out[position] != "\n":
                    out[position] = " "
            index = end
            continue

        # Double-quoted identifier.
        if char == '"':
            end = index + 1
            while end < len(sql):
                if sql[end] == '"':
                    if end + 1 < len(sql) and sql[end + 1] == '"':
                        end += 2
                        continue
                    end += 1
                    break
                end += 1
            else:
                end = len(sql)
            for position in range(index, end):
                if out[position] != "\n":
                    out[position] = " "
            index = end
            continue

        # Back-quoted identifier (MySQL style).
        if char == "`":
            end = index + 1
            while end < len(sql):
                if sql[end] == "`":
                    if end + 1 < len(sql) and sql[end + 1] == "`":
                        end += 2
                        continue
                    end += 1
                    break
                end += 1
            else:
                end = len(sql)
            for position in range(index, end):
                if out[position] != "\n":
                    out[position] = " "
            index = end
            continue

        # Square-bracket identifier.
        if char == "[":
            end = index + 1
            while end < len(sql) and sql[end] != "]":
                end += 1
            end = min(end + 1, len(sql))
            for position in range(index, end):
                if out[position] != "\n":
                    out[position] = " "
            index = end
            continue

        index += 1

    return "".join(out)


def _strip_trailing_semicolon(stripped: str) -> str:
    head = stripped.rstrip()
    if head.endswith(";"):
        head = head[:-1].rstrip()
    return head


def validate_read_only_query(query: str) -> None:
    """Reject every statement that could mutate the database.

    The check is applied to ``query`` with quoted regions and comments replaced
    by whitespace, so a quoted identifier ``"delete"`` is not a write keyword.
    Multi-statement queries (any non-trailing ``;`` outside a string/comment)
    are rejected too.
    """
    cleaned = query.strip()
    if not cleaned:
        raise UnsafeQueryError("Query is empty")

    # Strip quoted regions and comments once, on the original text. The
    # resulting string keeps the original character positions for ``;``, so
    # we can tell trailing ``;`` apart from a real statement separator.
    cleaned_for_count = _strip_for_safety(query)
    semicolon_count = cleaned_for_count.count(";")
    trailing_only = semicolon_count == 0 or (
        semicolon_count == 1 and cleaned_for_count.rstrip().endswith(";")
    )
    if not trailing_only:
        raise UnsafeQueryError("Only one statement is allowed")

    without_trailing = _strip_trailing_semicolon(cleaned)
    if not without_trailing:
        raise UnsafeQueryError("Query is empty")

    stripped = _strip_for_safety(without_trailing)
    if not stripped.strip():
        # A query that is *only* a quoted literal / comment carries no statement.
        raise UnsafeQueryError("Only SELECT or WITH queries are allowed")

    if _FORBIDDEN_LEAD.match(stripped) is not None:
        raise UnsafeQueryError(
            "Query starts with a forbidden operation; only SELECT and WITH are allowed"
        )
    if not re.match(r"^(select|with)\b", stripped, re.IGNORECASE):
        raise UnsafeQueryError("Only SELECT or WITH queries are allowed")
    if _FORBIDDEN_ANY.search(stripped):
        raise UnsafeQueryError("Query contains a forbidden operation")