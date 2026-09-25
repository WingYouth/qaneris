"""Federation trust boundaries and deterministic merge rules."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from qaneris.contracts import AskResponse, NormalizedResult
from qaneris.contracts.query import GroundedFieldRef, GroundedQueryPlan
from qaneris.federation.governance import FederationFailure
from qaneris.federation.merger import ControlledMerger
from qaneris.federation.models import (
    ExecutionScope,
    FederatedPlanDraft,
    JoinMapping,
    JoinMappingStatus,
    MergePlan,
)
from qaneris.federation.validator import FederatedPlanValidator
from qaneris.interfaces.api.app import create_app


def scope(**changes):
    return ExecutionScope(
        workspace_id="w", allowed_datasource_ids=["a", "b"],
        max_sources=2, max_rows_per_source=10, max_total_rows=20,
        max_intermediate_bytes=10000, deadline=datetime.now(UTC) + timedelta(minutes=2),
    ).model_copy(update=changes)


def mapping(**changes):
    return JoinMapping(
        mapping_id="m", workspace_id="w", business_key="订单号",
        left_datasource_id="a", left_data_object_id="ao", left_field_path="order_id",
        left_scan_version=1, left_grain="order",
        right_datasource_id="b", right_data_object_id="bo", right_field_path="order_id",
        right_scan_version=1, right_grain="order", cardinality="ONE_TO_ONE",
        null_policy="REJECT", status=JoinMappingStatus.CONFIRMED,
    ).model_copy(update=changes)


def response(source, rows, columns=None, *, time=False):
    columns = columns or (list(rows[0]) if rows else ["order_id"])
    ref = GroundedFieldRef(datasource_id=source, data_object_id=f"{source}o",
                           field_path="order_id")
    plan = GroundedQueryPlan.model_construct(
        datasource_id=source, selected_fields=[ref],
        time_field=GroundedFieldRef(datasource_id=source,
                                    data_object_id=f"{source}o", field_path="month")
        if time else None,
    )
    result = NormalizedResult(source=source, dataset="test", columns=columns,
                              rows=rows, row_count=len(rows))
    return AskResponse.model_construct(question="q", result=result, plan=plan)


def merge(operation, left, right, *, join_mode=None, scalar_operation=None,
          mapping_value=None, execution_scope=None):
    plan = MergePlan(operation=operation, input_task_ids=["source_1", "source_2"],
                     join_mode=join_mode, scalar_operation=scalar_operation,
                     mapping_id="m" if mapping_value else None)
    return ControlledMerger().merge(plan, {"source_1": left, "source_2": right},
                                    execution_scope or scope(), mapping_value)


def test_scalar_compare_combine_and_division_zero():
    a = response("a", [{"amount": 100}])
    b = response("b", [{"amount": 80}])
    assert merge("compare_scalars", a, b,
                 scalar_operation="difference").rows[0]["difference"] == 20
    assert merge("combine_scalars", a, b, scalar_operation="sum").rows[0]["sum"] == 180
    assert merge("combine_scalars", a, b, scalar_operation="ratio").rows[0]["ratio"] == 1.25
    assert merge("combine_scalars", a, b,
                 scalar_operation="percentage_change").rows[0]["percentage_change"] == 25
    assert merge("compare_scalars", a, b,
                 scalar_operation="percentage_difference").rows[0]["percentage_difference"] == 25
    with pytest.raises(FederationFailure, match="division_by_zero"):
        merge("combine_scalars", a, response("b", [{"amount": 0}]),
              scalar_operation="ratio")


def test_union_schema_and_budget():
    a = response("a", [{"x": 1}])
    b = response("b", [{"x": 2}])
    assert merge("union_rows", a, b).rows == [{"x": 1}, {"x": 2}]
    with pytest.raises(FederationFailure, match="merge_schema_mismatch"):
        merge("union_rows", a, response("b", [{"y": 2}]))
    with pytest.raises(FederationFailure, match="result_limit"):
        merge("union_rows", a, b, execution_scope=scope(max_total_rows=1))
    with pytest.raises(FederationFailure, match="result_limit"):
        merge("union_rows", a, b, execution_scope=scope(max_intermediate_bytes=2))


def test_time_axis_is_governed_and_missing_is_null():
    a = response("a", [{"month": "2026-01-01", "amount": 100},
                       {"month": "2026-02-01", "amount": 90}], time=True)
    b = response("b", [{"month": "2026-01-01", "amount": 80}], time=True)
    result = merge("align_time_series", a, b)
    assert result.rows[1] == {"time_key": "2026-02-01", "source_1_value": 90,
                              "source_2_value": None}
    with pytest.raises(FederationFailure, match="merge_time_axis_unconfirmed"):
        merge("align_time_series", a, response("b", [{"month": "2026-01-01", "amount": 80}]))


def test_keyed_join_modes_cardinality_null_and_type():
    a = response("a", [{"order_id": 1}, {"order_id": 2}, {"order_id": 3}])
    b = response("b", [{"order_id": 1}, {"order_id": 3}])
    m = mapping()
    assert merge("keyed_join", a, b, join_mode="inner", mapping_value=m).row_count == 2
    assert merge("keyed_join", a, b, join_mode="left", mapping_value=m).row_count == 3
    anti = merge("keyed_join", a, b, join_mode="anti", mapping_value=m)
    assert anti.rows == [{"left_order_id": 2}]
    with pytest.raises(FederationFailure, match="join_cardinality_violation"):
        merge("keyed_join", a, response("b", [{"order_id": 1}, {"order_id": 1}]),
              join_mode="inner", mapping_value=m)
    with pytest.raises(FederationFailure, match="join_null_key"):
        merge("keyed_join", a, response("b", [{"order_id": None}]),
              join_mode="inner", mapping_value=m)
    dropped = merge("keyed_join", a, response("b", [{"order_id": None}]),
                    join_mode="anti", mapping_value=m.model_copy(update={"null_policy": "DROP"}))
    assert dropped.warnings == ["right_null_keys_dropped:1"]
    assert merge("keyed_join", response("a", [{"order_id": 1}]),
                 response("b", [{"order_id": "1"}]),
                 join_mode="inner", mapping_value=m).row_count == 0


class Governance:
    def executable(self, mapping_id, workspace_id, sources):
        if mapping_id != "confirmed":
            raise FederationFailure("unconfirmed_mapping", blocked=True)
        return mapping()


def draft(**merge_changes):
    return FederatedPlanDraft.model_validate({
        "source_tasks": [
            {"datasource_id": "a", "question": "销售额是多少？", "purpose": "销售额",
             "expected_shape": "scalar"},
            {"datasource_id": "b", "question": "回款是多少？", "purpose": "回款",
             "expected_shape": "scalar"},
        ],
        "merge": {"operation": "compare_scalars", **merge_changes},
    })


def validate(candidate, execution_scope=None):
    return FederatedPlanValidator(Governance()).validate(
        candidate, execution_scope or scope(), "r", "比较销售额和回款", {}
    )


def test_plan_scope_refs_native_and_mapping():
    assert len(validate(draft()).source_tasks) == 2
    foreign = draft()
    foreign.source_tasks[1].datasource_id = "c"
    with pytest.raises(FederationFailure, match="forbidden_datasource"):
        validate(foreign)
    duplicate = draft()
    duplicate.source_tasks[1].datasource_id = "a"
    with pytest.raises(FederationFailure, match="duplicate_source_task"):
        validate(duplicate)
    native = draft()
    native.source_tasks[0].question = "SELECT * FROM orders"
    with pytest.raises(FederationFailure, match="native_query_forbidden"):
        validate(native)
    with pytest.raises(FederationFailure, match="unknown_source_task_reference"):
        validate(draft(input_task_ids=["bad"]))
    with pytest.raises(FederationFailure, match="unconfirmed_mapping"):
        validate(draft(operation="keyed_join", mapping_id="candidate", join_mode="anti"))
    with pytest.raises(FederationFailure, match="federation_budget_exceeded"):
        validate(draft(), scope(max_total_rows=5))
    with pytest.raises(ValueError):
        draft(operation="arbitrary_python")


def test_join_mapping_api_creates_candidate_only(tmp_path):
    client = TestClient(create_app(database_path=str(tmp_path / "catalog.db")))
    body = {
        "workspace_id": "w", "business_key": "订单号",
        "left_datasource_id": "a", "left_data_object_id": "ao",
        "left_field_path": "order_id", "left_grain": "order",
        "right_datasource_id": "b", "right_data_object_id": "bo",
        "right_field_path": "order_id", "right_grain": "order",
        "cardinality": "ONE_TO_ONE", "null_policy": "REJECT",
    }
    created = client.post("/api/join-mappings", json=body)
    assert created.status_code == 201
    mapping_id = created.json()["mapping_id"]
    assert created.json()["status"] == "CANDIDATE"
    listed = client.get("/api/join-mappings", params={"workspace_id": "w"})
    assert [item["mapping_id"] for item in listed.json()] == [mapping_id]
    assert client.post(f"/api/join-mappings/{mapping_id}/confirm",
                       json={"confirmed_by": "auditor"}).status_code == 400
    assert client.post(f"/api/join-mappings/{mapping_id}/reject").json()["status"] == "REJECTED"
