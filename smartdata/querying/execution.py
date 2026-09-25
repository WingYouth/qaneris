"""Formal parameterized execution boundary for grounded native queries."""

from __future__ import annotations

from smartdata.adapters import create_adapter
from smartdata.common.errors import QueryExecutionError
from smartdata.connections.ports import ConnectionProvider
from smartdata.contracts.datasource import Datasource
from smartdata.contracts.query import (
    ExecutionEvidence,
    GroundedExecution,
    GroundedQueryResult,
    NativeQuery,
)


class GroundedQueryExecutor:
    def __init__(self, connections: ConnectionProvider):
        self.connections = connections

    def execute(
        self, query: NativeQuery, datasource: Datasource, max_rows: int = 200
    ) -> GroundedExecution:
        if datasource.id != query.datasource_id:
            raise QueryExecutionError("Datasource does not match the native query")
        # The connection stays open for the duration of the query so a TLS-enabled datasource keeps
        # its certificate material alive while the adapter executes, and cleans it up after.
        with self.connections.open(datasource.id) as connection:
            adapter = create_adapter(datasource.id, datasource.kind, connection)
            try:
                native = adapter.execute_native(query.command, query.parameters, max_rows)
            except QueryExecutionError:
                raise
            except Exception as error:
                # Driver exceptions may echo bound values or connection URLs.
                raise QueryExecutionError("Native query execution failed") from error
        result = GroundedQueryResult(
            plan_id=query.plan_id,
            datasource_id=query.datasource_id,
            query_language=query.query_language,
            columns=native.columns,
            rows=native.rows,
            row_count=native.row_count,
            truncated=native.truncated,
            scan_version=query.scan_version,
        )
        evidence = ExecutionEvidence(
            plan_id=query.plan_id,
            datasource_id=query.datasource_id,
            scan_version=query.scan_version,
            query_language=query.query_language,
            display_command=query.display_command,
            row_count=result.row_count,
            truncated=result.truncated,
        )
        return GroundedExecution(result=result, evidence=evidence)
