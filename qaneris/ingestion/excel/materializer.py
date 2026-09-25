"""Atomic staging-SQLite materialization.

This module only writes a private staging database and proves its integrity. It never validates the
workbook, never re-reads it and never touches the catalog, the graph or the finalized artifact:

``BEGIN`` → ``CREATE TABLE`` → parameterized ``INSERT`` → ``COMMIT`` → ``PRAGMA integrity_check``
→ close writer → atomic rename.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qaneris.ingestion.excel.contracts import ExcelErrorCode, ExcelIngestionError
from qaneris.ingestion.excel.validator import ColumnPlan

_INSERT_BATCH = 500
_ARTIFACT_MODE = 0o600


@dataclass(frozen=True)
class TableWrite:
    """One table to materialize, with the row stream validated for it."""

    table_name: str
    columns: tuple[ColumnPlan, ...]
    row_count: int
    rows: Callable[[], Iterator[tuple[Any, ...]]]


def quote_identifier(name: str) -> str:
    """The only path by which a table or column name reaches SQL text."""
    if not name:
        raise ValueError("SQLite identifier cannot be empty")
    if "\x00" in name:
        raise ValueError("SQLite identifier cannot contain NUL")
    return '"' + name.replace('"', '""') + '"'


def create_table_sql(table: TableWrite) -> str:
    columns = ", ".join(
        f"{quote_identifier(column.column_name)} {column.inferred_type.value}"
        for column in table.columns
    )
    return f"CREATE TABLE {quote_identifier(table.table_name)} ({columns})"


def insert_sql(table: TableWrite) -> str:
    names = ", ".join(quote_identifier(column.column_name) for column in table.columns)
    placeholders = ", ".join("?" for _ in table.columns)
    return f"INSERT INTO {quote_identifier(table.table_name)} ({names}) VALUES ({placeholders})"


class SQLiteMaterializer:
    """Write one immutable SQLite artifact through a private staging file."""

    def materialize(self, tables: Sequence[TableWrite], destination: Path) -> Path:
        staging = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.staging")
        try:
            self._write(staging, tables)
            os.chmod(staging, _ARTIFACT_MODE)
            os.replace(staging, destination)
        except ExcelIngestionError:
            raise
        except Exception as error:
            raise ExcelIngestionError(
                ExcelErrorCode.MATERIALIZATION_FAILED,
                f"SQLite 物化失败（{type(error).__name__}）",
            ) from error
        finally:
            _discard(staging)
        return destination

    @staticmethod
    def is_finalized(destination: Path, table_names: Sequence[str]) -> bool:
        """True when ``destination`` is a readable database holding exactly the expected tables.

        The artifact directory is content-addressed by workspace + policy + exact bytes, and the
        finalize step is an atomic rename, so a present ``data.sqlite3`` should already be the
        finished artifact of these exact bytes. This check makes that an observation rather than an
        assumption: a missing, unreadable or shape-mismatched file reports ``False`` and is
        (re)materialized, while a complete one is left untouched by a retry.
        """
        if not destination.is_file():
            return False
        try:
            connection = sqlite3.connect(f"file:{destination}?mode=ro", uri=True)
        except sqlite3.Error:
            return False
        try:
            integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
            if integrity != ["ok"]:
                return False
            found = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        except sqlite3.Error:
            return False
        finally:
            connection.close()
        return found == set(table_names)

    @staticmethod
    def _write(staging: Path, tables: Sequence[TableWrite]) -> None:
        # Create the staging file with the final mode so data is never briefly world-readable.
        descriptor = os.open(staging, os.O_CREAT | os.O_EXCL | os.O_WRONLY, _ARTIFACT_MODE)
        os.close(descriptor)
        connection = sqlite3.connect(staging, isolation_level=None)
        try:
            connection.execute("BEGIN")
            try:
                for table in tables:
                    connection.execute(create_table_sql(table))
                    inserted = _insert_rows(connection, table)
                    if inserted != table.row_count:
                        raise ExcelIngestionError(
                            ExcelErrorCode.MATERIALIZATION_FAILED,
                            f"表 {table.table_name} 写入 {inserted} 行，与校验结果 "
                            f"{table.row_count} 行不一致",
                        )
                connection.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    connection.execute("ROLLBACK")
                raise
            integrity = [row[0] for row in connection.execute("PRAGMA integrity_check").fetchall()]
        finally:
            connection.close()
        if integrity != ["ok"]:
            raise ExcelIngestionError(
                ExcelErrorCode.INTEGRITY_CHECK_FAILED,
                f"SQLite integrity_check 未通过：{'/'.join(str(item) for item in integrity)}",
            )


def _insert_rows(connection: sqlite3.Connection, table: TableWrite) -> int:
    statement = insert_sql(table)
    inserted = 0
    batch: list[tuple[Any, ...]] = []
    for row in table.rows():
        batch.append(row)
        if len(batch) >= _INSERT_BATCH:
            connection.executemany(statement, batch)
            inserted += len(batch)
            batch = []
    if batch:
        connection.executemany(statement, batch)
        inserted += len(batch)
    return inserted


def _discard(staging: Path) -> None:
    """Remove an unfinished staging database and any journal side files."""
    for suffix in ("", "-journal", "-wal", "-shm"):
        candidate = Path(f"{staging}{suffix}")
        with contextlib.suppress(OSError):
            if candidate.exists():
                candidate.unlink()
