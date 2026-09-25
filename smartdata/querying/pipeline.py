"""Constrained query preparation pipeline."""

from __future__ import annotations

from smartdata.catalog.ports import ProfileCatalogReader
from smartdata.contracts.query import PreparedQuery
from smartdata.querying.generation.generator import QueryGenerator
from smartdata.querying.planning.planner import RuleBasedQueryPlanner
from smartdata.querying.retrieval import (
    ProfileRetriever,
    QueryIntentParser,
    RuleBasedQueryIntentParser,
)
from smartdata.querying.validation.plans import NativeQueryValidator, QueryPlanValidator


class QueryPreparationPipeline:
    def __init__(
        self,
        catalog: ProfileCatalogReader,
        intent_parser: QueryIntentParser | None = None,
        retriever: ProfileRetriever | None = None,
        planner: RuleBasedQueryPlanner | None = None,
        generator: QueryGenerator | None = None,
        plan_validator: QueryPlanValidator | None = None,
        native_validator: NativeQueryValidator | None = None,
    ):
        self.catalog = catalog
        self.intent_parser = intent_parser or RuleBasedQueryIntentParser()
        self.retriever = retriever or ProfileRetriever()
        self.planner = planner or RuleBasedQueryPlanner()
        self.generator = generator or QueryGenerator()
        self.plan_validator = plan_validator or QueryPlanValidator()
        self.native_validator = native_validator or NativeQueryValidator()

    def prepare(
        self,
        question: str,
        workspace_id: str = "default",
        requested_datasource_id: str | None = None,
        max_rows: int = 200,
    ) -> PreparedQuery:
        intent = self.intent_parser.parse(question, requested_datasource_id)
        profile = self.catalog.get_company_data_profile(workspace_id)
        context = self.retriever.retrieve(profile, intent)
        plan = self.planner.plan(context, max_rows)
        self.plan_validator.validate(plan, context)
        generated = self.generator.generate(plan, context)
        self.native_validator.validate(generated, plan)
        return PreparedQuery(
            intent=intent,
            context=context,
            plan=plan,
            generated_query=generated,
        )
