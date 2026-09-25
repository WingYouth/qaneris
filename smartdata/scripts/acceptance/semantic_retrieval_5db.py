from __future__ import annotations

import argparse
import json
from pathlib import Path

from smartdata.catalog import Catalog
from smartdata.contracts import BusinessQuery, CompanyDataProfile, DataSourceProfile, ScanStatus
from smartdata.contracts.semantic import SemanticCandidate
from smartdata.semantic import MetadataLexicalSemanticRetriever, SemanticRetrievalResult

EXPECTED_DATASOURCES = {
    "acceptance-sqlite",
    "acceptance-postgresql",
    "acceptance-mysql",
    "acceptance-mongodb",
    "acceptance-redis",
}
RELATIONAL_RELATIONSHIP_SOURCES = {
    "acceptance-sqlite",
    "acceptance-postgresql",
    "acceptance-mysql",
}
BUSINESS_QUESTIONS = {
    "实收销售额": {"pay_amount"},
    "销售额": {"pay_amount", "total_amount"},
    "地区": {"region"},
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate P1-03 semantic retrieval against a scanned five-database catalog."
    )
    parser.add_argument("--catalog", required=True, type=Path, help="Acceptance Catalog path")
    return parser.parse_args()


def objects(source: DataSourceProfile):
    return [item for namespace in source.namespaces for item in namespace.data_objects]


def print_candidate(candidate: SemanticCandidate) -> None:
    print(f"  - asset_type: {candidate.asset_type.value}")
    print(f"    name: {candidate.name}")
    print(f"    score: {candidate.score:.6f}")
    print(f"    datasource_id: {candidate.datasource_id}")
    print(f"    data_object_id: {candidate.data_object_id}")
    print(f"    field_path: {candidate.field_path}")
    print(f"    relationship_id: {candidate.relationship_id}")
    print(f"    matched_terms: {candidate.matched_terms}")
    print(f"    reasons: {candidate.reasons}")
    print(f"    ambiguous: {candidate.ambiguous}")


def run_query(
    retriever: MetadataLexicalSemanticRetriever,
    profile: CompanyDataProfile,
    question: str,
    requested_source: DataSourceProfile | None,
    *,
    limit: int = 20,
) -> SemanticRetrievalResult:
    requested_id = requested_source.datasource_id if requested_source else None
    result = retriever.retrieve(
        profile,
        BusinessQuery(question=question, objective="lookup", entities=[question]),
        requested_datasource_id=requested_id,
        limit=limit,
    )
    requested_label = (
        f"{requested_source.name} ({requested_id})" if requested_source else "<all>"
    )
    print()
    print(f"Question: {question}")
    print(f"Requested DataSource: {requested_label}")
    print(f"Candidate Count: {len(result.candidates)}")
    for candidate in result.candidates:
        print_candidate(candidate)
    for warning in result.warnings:
        print(f"  warning: {warning}")
    return result


def isolated(result: SemanticRetrievalResult, datasource_id: str) -> bool:
    return all(item.datasource_id == datasource_id for item in result.candidates)


def physical_retrieval_checks(
    retriever: MetadataLexicalSemanticRetriever,
    profile: CompanyDataProfile,
    sources: dict[str, DataSourceProfile],
) -> tuple[bool, bool, list[SemanticRetrievalResult]]:
    cases = [
        ("acceptance-sqlite", "orders", "data_object", "orders"),
        ("acceptance-postgresql", "pay_amount", "field", "pay_amount"),
        ("acceptance-mysql", "customer_id", "field", "customer_id"),
        ("acceptance-mongodb", "orders", "data_object", "orders"),
    ]
    physical_ok = True
    isolation_ok = True
    results: list[SemanticRetrievalResult] = []
    for source_name, question, asset_type, expected_name in cases:
        source = sources.get(source_name)
        if source is None:
            print(f"[FAIL] Missing required datasource: {source_name}")
            physical_ok = False
            isolation_ok = False
            continue
        result = run_query(retriever, profile, question, source, limit=100)
        results.append(result)
        matching = [
            item
            for item in result.candidates
            if item.asset_type == asset_type
            and (
                item.name == expected_name
                or item.field_path == expected_name
            )
        ]
        located = all(item.data_object_id for item in matching)
        case_ok = bool(matching) and located
        case_isolated = isolated(result, source.datasource_id)
        print(f"[{'PASS' if case_ok else 'FAIL'}] Physical metadata: {source_name}")
        print(f"[{'PASS' if case_isolated else 'FAIL'}] Datasource isolation: {source_name}")
        physical_ok &= case_ok
        isolation_ok &= case_isolated

    redis = sources.get("acceptance-redis")
    if redis is None or not objects(redis):
        print("[FAIL] Redis profile has no real data object")
        physical_ok = False
        isolation_ok = False
    else:
        redis_object = objects(redis)[0]
        result = run_query(retriever, profile, redis_object.name, redis, limit=100)
        results.append(result)
        matching = [
            item
            for item in result.candidates
            if item.asset_type == "data_object"
            and item.name == redis_object.name
            and item.data_object_id == redis_object.id
        ]
        case_ok = bool(matching)
        case_isolated = isolated(result, redis.datasource_id)
        print(f"[{'PASS' if case_ok else 'FAIL'}] Physical metadata: acceptance-redis")
        print(
            f"[{'PASS' if case_isolated else 'FAIL'}] "
            "Datasource isolation: acceptance-redis"
        )
        physical_ok &= case_ok
        isolation_ok &= case_isolated
    return physical_ok, isolation_ok, results


def relationship_check(
    retriever: MetadataLexicalSemanticRetriever,
    profile: CompanyDataProfile,
    sources: dict[str, DataSourceProfile],
) -> tuple[bool, SemanticRetrievalResult | None]:
    allowed_ids = {
        sources[name].datasource_id
        for name in RELATIONAL_RELATIONSHIP_SOURCES
        if name in sources
    }
    relationship = next(
        (
            item
            for item in profile.relationships
            if item.from_datasource_id in allowed_ids
            and item.to_datasource_id == item.from_datasource_id
        ),
        None,
    )
    if relationship is None:
        print("[FAIL] SQLite/PostgreSQL/MySQL profiles contain no relationship")
        return False, None
    source = next(
        item
        for item in sources.values()
        if item.datasource_id == relationship.from_datasource_id
    )
    terms = [
        relationship.relationship_type,
        relationship.from_field_path or "",
        relationship.to_field_path or "",
    ]
    question = " ".join(item for item in terms if item)
    result = run_query(retriever, profile, question, source, limit=100)
    matching = [
        item
        for item in result.candidates
        if item.asset_type == "relationship"
        and item.relationship_id == relationship.id
    ]
    passed = bool(matching) and isolated(result, source.datasource_id)
    print(f"[{'PASS' if passed else 'FAIL'}] Relationship ID: {relationship.id}")
    return passed, result


def boundary_checks(
    retriever: MetadataLexicalSemanticRetriever,
    profile: CompanyDataProfile,
    sources: dict[str, DataSourceProfile],
) -> tuple[bool, bool, bool, list[SemanticRetrievalResult]]:
    postgres = sources.get("acceptance-postgresql")
    if postgres is None:
        print("[FAIL] PostgreSQL datasource is missing")
        empty = retriever.retrieve(
            profile,
            BusinessQuery(question="no match", objective="lookup"),
            limit=3,
        )
        return False, False, False, [empty]

    isolation_result = run_query(retriever, profile, "customer_id", postgres, limit=100)
    isolation_ok = bool(isolation_result.candidates) and isolated(
        isolation_result, postgres.datasource_id
    )

    limit_result = run_query(retriever, profile, "customer_id", postgres, limit=3)
    limit_ok = len(limit_result.candidates) <= 3

    no_match_result = run_query(
        retriever,
        profile,
        "**semantic_retrieval_no_match_987654**",
        None,
    )
    no_match_ok = not no_match_result.candidates and bool(no_match_result.warnings)
    return isolation_ok, limit_ok, no_match_ok, [
        isolation_result,
        limit_result,
        no_match_result,
    ]


def sample_leakage_check(
    profile: CompanyDataProfile, results: list[SemanticRetrievalResult]
) -> bool:
    serialized = "\n".join(item.model_dump_json() for item in results)
    if '"sample_values"' in serialized or '"previews"' in serialized:
        return False
    profile_fragments: list[str] = []
    for source in profile.data_sources:
        for data_object in objects(source):
            profile_fragments.extend(
                json.dumps(preview.values, ensure_ascii=False, sort_keys=True)
                for preview in data_object.previews
            )
            profile_fragments.extend(
                json.dumps(field.sample_values, ensure_ascii=False, sort_keys=True)
                for field in data_object.fields
                if field.sample_values
            )
    return not any(fragment in serialized for fragment in profile_fragments if fragment not in {"[]", "{}"})


def observe_business_semantics(
    retriever: MetadataLexicalSemanticRetriever,
    profile: CompanyDataProfile,
) -> list[SemanticRetrievalResult]:
    print()
    print("=" * 80)
    print("Business Semantic Retrieval (OBSERVATION only)")
    print("=" * 80)
    results: list[SemanticRetrievalResult] = []
    for question, expected_fields in BUSINESS_QUESTIONS.items():
        result = run_query(retriever, profile, question, None, limit=100)
        results.append(result)
        matched_fields = {
            item.field_path
            for item in result.candidates
            if item.field_path in expected_fields
        }
        if matched_fields:
            print(
                f"[OBSERVATION] {question}: mapped to physical fields "
                f"{sorted(matched_fields)}"
            )
        else:
            print(
                "[OBSERVATION] 当前真实 Scan Profile 没有相应 SemanticMetadata，"
                "因此业务术语暂时无法映射到物理字段。"
            )
    return results


def main() -> int:
    args = parse_args()
    catalog_path = args.catalog.expanduser().resolve()
    if not catalog_path.is_file():
        print(f"[FAIL] Catalog file does not exist: {catalog_path}")
        return 2

    catalog = Catalog(str(catalog_path))
    profile = catalog.get_company_data_profile()
    sources = {item.name: item for item in profile.data_sources}

    print(f"Catalog: {catalog_path}")
    print(f"Workspace: {profile.workspace_id}")
    print()
    print("=" * 80)
    print("Scanned DataSources")
    print("=" * 80)
    snapshot_ok = True
    for source in profile.data_sources:
        data_objects = objects(source)
        field_count = sum(len(item.fields) for item in data_objects)
        relationship_count = sum(
            1
            for item in profile.relationships
            if source.datasource_id
            in {item.from_datasource_id, item.to_datasource_id}
        )
        snapshot = catalog.get_active_snapshot(source.datasource_id)
        current_snapshot_ok = bool(
            snapshot
            and snapshot.status == ScanStatus.READY
            and snapshot.profile is not None
        )
        snapshot_ok &= current_snapshot_ok
        print(f"- datasource name: {source.name}")
        print(f"  datasource_id: {source.datasource_id}")
        print(f"  kind: {source.kind.value}")
        print(f"  driver: {source.driver}")
        print(f"  namespace count: {len(source.namespaces)}")
        print(f"  data object count: {len(data_objects)}")
        print(f"  field count: {field_count}")
        print(f"  relationship count: {relationship_count}")
        print(f"  active ScanSnapshot: {'PASS' if current_snapshot_ok else 'FAIL'}")

    datasource_set_ok = len(profile.data_sources) == 5 and set(sources) == EXPECTED_DATASOURCES
    print()
    print(
        f"[{'PASS' if datasource_set_ok else 'FAIL'}] Expected exactly five datasources: "
        f"{sorted(EXPECTED_DATASOURCES)}"
    )
    print(f"[{'PASS' if snapshot_ok else 'FAIL'}] Active ready ScanSnapshot for every source")

    retriever = MetadataLexicalSemanticRetriever()
    physical_ok, physical_isolation_ok, physical_results = physical_retrieval_checks(
        retriever, profile, sources
    )
    relationship_ok, relationship_result = relationship_check(retriever, profile, sources)
    boundary_isolation_ok, limit_ok, no_match_ok, boundary_results = boundary_checks(
        retriever, profile, sources
    )
    business_results = observe_business_semantics(retriever, profile)
    leakage_ok = sample_leakage_check(
        profile,
        [
            *physical_results,
            *([relationship_result] if relationship_result else []),
            *boundary_results,
            *business_results,
        ],
    )
    isolation_ok = physical_isolation_ok and boundary_isolation_ok

    hard_results = {
        "Physical Retrieval": datasource_set_ok and snapshot_ok and physical_ok,
        "Datasource Isolation": isolation_ok,
        "Relationship Retrieval": relationship_ok,
        "No Match Behavior": no_match_ok,
        "Limit": limit_ok,
        "Sample Leakage": leakage_ok,
    }
    print()
    print("=" * 80)
    print("Acceptance Summary")
    print("=" * 80)
    for name, passed in hard_results.items():
        print(f"{name}: {'PASS' if passed else 'FAIL'}")
    print("Business Semantic Retrieval: OBSERVATION")
    return 0 if all(hard_results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
