"""Independent safety, provenance and revision checks for formal native reads."""

from __future__ import annotations

from qaneris.common.errors import ExecutionValidationError, QuerySafetyError
from qaneris.contracts.query import GroundedQueryPlan, NativeQuery, QueryLanguage
from qaneris.querying.validation.read_only import _strip_for_safety, validate_read_only_query

_MONGO_STAGES = {"$match", "$group", "$project", "$sort", "$limit", "$count"}
_MONGO_FORBIDDEN = {"$lookup", "$out", "$merge", "$where", "$function", "$accumulator"}
_REDIS_READS = {"GET", "MGET", "HGETALL", "LRANGE", "SMEMBERS", "ZRANGE", "SCAN",
                "EXISTS", "TYPE", "TTL", "SCARD", "ZCARD"}


class GroundedNativeQueryValidator:
    def validate(
        self,
        query: NativeQuery,
        plan: GroundedQueryPlan,
        *,
        expected: NativeQuery | None = None,
    ) -> None:
        """Validate the standalone query and, optionally, its provenance against ``expected``.

        Provenance compares the six authoritative fields that fully determine a
        read-only native query: ``datasource_id``, ``plan_id``, ``scan_version``,
        ``query_language``, ``command`` and ``parameters``. The compiled
        ``expected`` is provided by the orchestration entry point so a forged
        ``NativeQuery`` (same identifiers, different command) cannot bypass
        execution even when the SQL itself is read-only.
        """
        if query.datasource_id != plan.datasource_id or query.plan_id != plan.plan_id:
            raise QuerySafetyError("Native query does not belong to the validated plan")
        if query.scan_version != plan.scan_version:
            raise QuerySafetyError("Native query scan version does not match its plan")
        if expected is not None and query.query_language is not expected.query_language:
            raise QuerySafetyError("Native query language does not match the compiler")
        if query.query_language is QueryLanguage.SQL:
            if not isinstance(query.command, str):
                raise QuerySafetyError("SQL command must be a string")
            validate_read_only_query(query.command)
            driver = "postgresql" if "%s" in _strip_for_safety(query.command) else "sqlite"
            if expected is not None and isinstance(expected.command, str):
                driver = "postgresql" if "%s" in _strip_for_safety(expected.command) else "sqlite"
            if _placeholder_count(query.command, driver) != len(query.parameters):
                raise QuerySafetyError("SQL placeholder count does not match parameter count")
        elif query.query_language is QueryLanguage.MONGODB_JSON:
            self._validate_mongo(query, plan)
        elif query.query_language is QueryLanguage.REDIS_JSON:
            self._validate_redis(query, plan)
        else:
            raise QuerySafetyError("Formal grounded execution does not support this language")
        if expected is not None:
            _check_provenance(query, expected)
            if query.display_command != expected.display_command:
                raise QuerySafetyError("Native query public display differs from compiler output")

    @staticmethod
    def _validate_mongo(query: NativeQuery, plan: GroundedQueryPlan) -> None:
        from qaneris.querying.generation.grounded_mongo import GroundedMongoCompiler

        command = query.command
        if not isinstance(command, dict) or query.parameters:
            raise QuerySafetyError("MongoDB requires a typed command without parameters")
        if command.get("operation") not in {"find", "aggregate"}:
            raise QuerySafetyError("MongoDB operation is not allowed")
        locator = plan.data_objects.get(plan.data_object_ids[0])
        if len(plan.data_object_ids) != 1 or plan.joins or locator is None or command.get("collection") != locator.name:
            raise QuerySafetyError("MongoDB collection is outside the plan")
        if command["operation"] == "find":
            if not isinstance(command.get("limit"), int) or not 1 <= command["limit"] <= plan.limit:
                raise QuerySafetyError("MongoDB find limit is invalid")
        else:
            pipeline = command.get("pipeline")
            if not isinstance(pipeline, list) or not pipeline:
                raise QuerySafetyError("MongoDB pipeline is invalid")
            for stage in pipeline:
                if not isinstance(stage, dict) or len(stage) != 1 or next(iter(stage)) not in _MONGO_STAGES:
                    raise QuerySafetyError("MongoDB stage is not allowed")
            if pipeline[-1] != {"$limit": plan.limit}:
                raise QuerySafetyError("MongoDB aggregate limit is invalid")
        def walk(value: object) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    if str(key) in _MONGO_FORBIDDEN:
                        raise QuerySafetyError("MongoDB operator is forbidden")
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
        walk(command)
        _check_provenance(query, GroundedMongoCompiler().compile(plan))

    @staticmethod
    def _validate_redis(query: NativeQuery, plan: GroundedQueryPlan) -> None:
        from qaneris.querying.generation.grounded_redis import GroundedRedisCompiler

        command = query.command
        if not isinstance(command, dict) or query.parameters:
            raise QuerySafetyError("Redis requires a typed command without parameters")
        op, args = command.get("command"), command.get("args")
        if op not in _REDIS_READS or not isinstance(args, list):
            raise QuerySafetyError("Redis command is not allowed")
        if op == "SCAN":
            if args or not isinstance(command.get("limit"), int) or not 1 <= command["limit"] <= plan.limit:
                raise QuerySafetyError("Redis SCAN is unbounded")
        elif op in {"LRANGE", "ZRANGE"}:
            if len(args) != 3 or args[1] != 0 or not isinstance(args[2], int) or args[2] >= plan.limit:
                raise QuerySafetyError("Redis range is unbounded")
        elif not args:
            raise QuerySafetyError("Redis exact-key command needs a key")
        _check_provenance(query, GroundedRedisCompiler().compile(plan))


def _check_provenance(query: NativeQuery, expected: NativeQuery) -> None:
    """Refuse any drift between the produced ``query`` and ``expected``."""
    if query.datasource_id != expected.datasource_id:
        raise QuerySafetyError(
            "Native query provenance mismatch (datasource_id) "
            f"expected={expected.datasource_id!r} got={query.datasource_id!r}"
        )
    if query.plan_id != expected.plan_id:
        raise QuerySafetyError(
            "Native query provenance mismatch (plan_id) "
            f"expected={expected.plan_id!r} got={query.plan_id!r}"
        )
    if query.scan_version != expected.scan_version:
        raise QuerySafetyError(
            "Native query provenance mismatch (scan_version) "
            f"expected={expected.scan_version!r} got={query.scan_version!r}"
        )
    if query.query_language is not expected.query_language:
        raise QuerySafetyError(
            "Native query provenance mismatch (query_language) "
            f"expected={expected.query_language!r} got={query.query_language!r}"
        )
    if query.command != expected.command:
        raise QuerySafetyError(
            "Native query provenance mismatch (command); "
            "the compiled native query must come from the validated plan"
        )
    if tuple(query.parameters) != tuple(expected.parameters):
        raise QuerySafetyError(
            "Native query provenance mismatch (parameters); "
            "parameter ordering must match the compiled native query"
        )


class ExecutionRevisionValidator:
    def validate(self, query: NativeQuery, current_scan_version: int | None) -> None:
        if query.scan_version is None or current_scan_version is None:
            raise ExecutionValidationError(
                "A published graph scan_version is required for execution"
            )
        if query.scan_version != current_scan_version:
            raise ExecutionValidationError(
                "The query plan was built from a stale graph revision: "
                f"plan={query.scan_version}, current={current_scan_version}"
            )


def _placeholder_count(sql: str, driver: str = "sqlite") -> int:
    """Count the target DBAPI placeholders outside SQL literals and comments."""
    stripped = _strip_for_safety(sql)
    qmarks = stripped.count("?")
    percents = stripped.count("%s")
    if driver == "sqlite":
        if percents:
            raise QuerySafetyError("SQL placeholder dialect mismatch")
        return qmarks
    if qmarks:
        raise QuerySafetyError("SQL placeholder dialect mismatch")
    return percents
