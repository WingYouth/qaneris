from __future__ import annotations

import argparse
from pathlib import Path

from smartdata.catalog import Catalog
from smartdata.contracts import BusinessQuery, SemanticAssetType
from smartdata.semantic import (
    MetadataLexicalSemanticRetriever,
    SemanticAssetBootstrap,
    SQLiteSemanticAssetRegistry,
)

EXPECTED_DATASOURCES = {
    "acceptance-postgresql",
    "acceptance-mysql",
    "acceptance-sqlite",
    "acceptance-mongodb",
    "acceptance-redis",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Bootstrap and validate governed semantic assets "
            "across the real five-database Acceptance Catalog."
        )
    )
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument(
        "--seed",
        type=Path,
        default=Path(__file__).with_name("semantic_assets_retail.json"),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    catalog_path = args.catalog.expanduser().resolve()
    seed_path = args.seed.expanduser().resolve()

    if not catalog_path.is_file():
        print(f"[FAIL] Catalog does not exist: {catalog_path}")
        return 2

    if not seed_path.is_file():
        print(f"[FAIL] Semantic asset seed does not exist: {seed_path}")
        return 2

    catalog = Catalog(catalog_path)
    profile = catalog.get_company_data_profile()

    datasources = {
        datasource.name: datasource.datasource_id
        for datasource in profile.data_sources
    }

    missing = EXPECTED_DATASOURCES - set(datasources)

    if missing:
        print(
            "[FAIL] Five-database Acceptance Catalog is incomplete. "
            f"Missing: {', '.join(sorted(missing))}"
        )
        return 2

    print(f"Catalog: {catalog_path}")
    print(f"Seed: {seed_path}")
    print(f"Data Sources: {len(profile.data_sources)}")

    registry = SQLiteSemanticAssetRegistry(catalog_path)

    loaded = SemanticAssetBootstrap(registry).load_file(
        profile,
        seed_path,
    )

    print()
    print("=" * 72)
    print("Semantic Asset Bootstrap")
    print("=" * 72)
    print(f"Loaded Assets: {len(loaded)}")

    for asset in loaded:
        print(
            f"- {asset.asset_id}: "
            f"kind={asset.kind.value}, "
            f"status={asset.status.value}, "
            f"trusted={asset.trusted}, "
            f"source={asset.source.value}, "
            f"binding="
            f"{asset.datasource_id}/"
            f"{asset.data_object_id}/"
            f"{asset.field_path or '-'}"
        )

    retriever = MetadataLexicalSemanticRetriever()

    trusted_profile = registry.project_profile(profile)
    exploratory_profile = registry.project_profile(
        profile,
        include_untrusted=True,
    )

    checks: list[bool] = []

    retrieval_checks = [
        (
            "acceptance-postgresql",
            "PostgreSQL实收销售额",
            SemanticAssetType.METRIC,
            "pay_amount",
            "metric",
        ),
        (
            "acceptance-mysql",
            "MySQL实收销售额",
            SemanticAssetType.METRIC,
            "pay_amount",
            "metric",
        ),
        (
            "acceptance-sqlite",
            "SQLite实收销售额",
            SemanticAssetType.METRIC,
            "pay_amount",
            "metric",
        ),
        (
            "acceptance-mongodb",
            "MongoDB实收销售额",
            SemanticAssetType.METRIC,
            "pay_amount",
            "metric",
        ),
        (
            "acceptance-redis",
            "Redis商品缓存",
            SemanticAssetType.BUSINESS_TERM,
            None,
            "entity",
        ),
    ]

    print()
    print("=" * 72)
    print("5 DB Scoped Semantic Retrieval")
    print("=" * 72)

    for (
        datasource_name,
        question,
        asset_type,
        field_path,
        query_kind,
    ) in retrieval_checks:
        datasource_id = datasources[datasource_name]

        query_kwargs = {}

        if query_kind == "metric":
            query_kwargs["metrics"] = [question]
        else:
            query_kwargs["entities"] = [question]

        result = retriever.retrieve(
            trusted_profile,
            BusinessQuery(
                question=question,
                objective="lookup",
                **query_kwargs,
            ),
            requested_datasource_id=datasource_id,
        )

        matches = [
            item
            for item in result.candidates
            if item.asset_type == asset_type
            and item.datasource_id == datasource_id
            and (
                field_path is None
                or item.field_path == field_path
            )
        ]

        passed = bool(matches)
        checks.append(passed)

        print(
            f"[{'PASS' if passed else 'FAIL'}] "
            f"{datasource_name}: {question}"
        )

        if matches:
            candidate = matches[0]
            print(
                f"       candidate={candidate.name}, "
                f"type={candidate.asset_type.value}, "
                f"field={candidate.field_path or '-'}, "
                f"score={candidate.score}"
            )

    print()
    print("=" * 72)
    print("Trusted / Exploratory Governance")
    print("=" * 72)

    mongo_id = datasources["acceptance-mongodb"]

    suggestion_query = BusinessQuery(
        question="MongoDB模型建议销售总额",
        objective="lookup",
        metrics=["MongoDB模型建议销售总额"],
    )

    trusted_suggestion = retriever.retrieve(
        trusted_profile,
        suggestion_query,
        requested_datasource_id=mongo_id,
    )

    exploratory_suggestion = retriever.retrieve(
        exploratory_profile,
        suggestion_query,
        requested_datasource_id=mongo_id,
    )

    suggestion_hidden = not any(
        item.asset_type == SemanticAssetType.METRIC
        and item.name == "MongoDB模型建议销售总额"
        for item in trusted_suggestion.candidates
    )

    suggestion_visible = any(
        item.asset_type == SemanticAssetType.METRIC
        and item.name == "MongoDB模型建议销售总额"
        for item in exploratory_suggestion.candidates
    )

    checks.extend(
        [
            suggestion_hidden,
            suggestion_visible,
        ]
    )

    print(
        f"[{'PASS' if suggestion_hidden else 'FAIL'}] "
        "model suggestion excluded from Trusted Path"
    )

    print(
        f"[{'PASS' if suggestion_visible else 'FAIL'}] "
        "model suggestion available in Exploratory Path"
    )

    print()
    print("=" * 72)

    passed = all(checks)

    print(
        "5 DB Semantic Bootstrap Acceptance: "
        f"{'PASS' if passed else 'FAIL'}"
    )

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
