"""Small, locked dispatch for formal grounded execution."""

from __future__ import annotations

from qaneris.common.errors import QueryPlanningError
from qaneris.contracts.datasource import Datasource, DatasourceKind
from qaneris.contracts.query import GroundedQueryPlan, NativeQuery
from qaneris.querying.generation.grounded_mongo import GroundedMongoCompiler
from qaneris.querying.generation.grounded_redis import GroundedRedisCompiler
from qaneris.querying.generation.grounded_sql import GroundedSQLCompiler


class GroundedNativeCompiler:
    def __init__(self) -> None:
        self.sql = GroundedSQLCompiler()
        self.mongo = GroundedMongoCompiler()
        self.redis = GroundedRedisCompiler()

    def compile(self, plan: GroundedQueryPlan, datasource: Datasource) -> NativeQuery:
        if datasource.id != plan.datasource_id or datasource.workspace_id != plan.workspace_id:
            raise QueryPlanningError("Datasource does not match the grounded plan")
        target = (datasource.kind, datasource.driver)
        if target in {(DatasourceKind.RELATIONAL, "sqlite"),
                      (DatasourceKind.RELATIONAL, "postgresql"),
                      (DatasourceKind.RELATIONAL, "mysql")}:
            return self.sql.compile(plan, driver=datasource.driver)
        if target == (DatasourceKind.DOCUMENT, "mongodb"):
            return self.mongo.compile(plan)
        if target == (DatasourceKind.KEY_VALUE, "redis"):
            return self.redis.compile(plan)
        raise QueryPlanningError("formal grounded execution is not enabled for this driver")
