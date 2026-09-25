"""Pure, bounded and deterministic merge operations over completed Ask responses."""

import json
import re
from collections import Counter
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from qaneris.contracts import AskResponse
from qaneris.contracts.query import GroundedQueryPlan
from qaneris.federation.governance import FederationFailure
from qaneris.federation.models import ExecutionScope, JoinMapping, MergedResult, MergePlan


def _number(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise FederationFailure("merge_input_invalid")
    try:
        number = Decimal(str(value))
    except InvalidOperation as error:
        raise FederationFailure("merge_input_invalid") from error
    if not number.is_finite():
        raise FederationFailure("merge_input_invalid")
    return number


def _plain(number: Decimal) -> int | float:
    return int(number) if number == number.to_integral_value() else float(number)


def _rows(response: AskResponse) -> list[dict[str, Any]]:
    if response.result is None:
        raise FederationFailure("merge_input_invalid")
    return response.result.rows


def _column(rows: list[dict[str, Any]], path: str) -> str:
    if not rows:
        return path
    columns = set(rows[0])
    matches = [item for item in (path, path.rsplit(".", 1)[-1]) if item in columns]
    if not matches:
        raise FederationFailure("merge_input_invalid")
    return matches[0]


class ControlledMerger:
    def merge(
        self, plan: MergePlan, responses: dict[str, AskResponse], scope: ExecutionScope,
        mapping: JoinMapping | None = None,
    ) -> MergedResult:
        if datetime.now(scope.deadline.tzinfo) >= scope.deadline:
            raise FederationFailure("federation_deadline_exceeded")
        inputs = [responses[task_id] for task_id in plan.input_task_ids]
        total_rows = 0
        total_bytes = 0
        for response in inputs:
            rows = _rows(response)
            total_rows += len(rows)
            if len(rows) > scope.max_rows_per_source or total_rows > scope.max_total_rows:
                raise FederationFailure("result_limit")
            total_bytes += len(json.dumps(rows, ensure_ascii=False, default=str).encode())
            if total_bytes > scope.max_intermediate_bytes:
                raise FederationFailure("result_limit")
        if plan.operation in {"compare_scalars", "combine_scalars"}:
            result = self._scalars(plan, inputs)
        elif plan.operation == "union_rows":
            result = self._union(inputs)
        elif plan.operation == "align_time_series":
            result = self._timeseries(inputs)
        elif plan.operation == "keyed_join" and mapping is not None:
            result = self._join(inputs, plan, mapping)
        else:
            raise FederationFailure("unsupported_merge_operation", blocked=True)
        if result.row_count > scope.max_total_rows:
            raise FederationFailure("result_limit")
        if len(json.dumps(result.rows, ensure_ascii=False, default=str).encode()) \
                > scope.max_intermediate_bytes:
            raise FederationFailure("result_limit")
        return result.model_copy(update={
            "operation": plan.operation,
            "truncated": any(bool(item.result and item.result.truncated) for item in inputs),
        })

    @staticmethod
    def _scalars(plan: MergePlan, inputs: list[AskResponse]) -> MergedResult:
        values: list[Decimal] = []
        labels: list[str] = []
        for index, response in enumerate(inputs):
            rows = _rows(response)
            if len(rows) != 1 or len(rows[0]) != 1:
                raise FederationFailure("merge_input_invalid")
            label, raw = next(iter(rows[0].items()))
            values.append(_number(raw))
            labels.append(f"source_{index + 1}_{label}")
        row = {label: _plain(value) for label, value in zip(labels, values, strict=True)}
        operation = plan.scalar_operation
        if operation:
            if operation == "sum":
                calculated = sum(values)
            elif operation == "difference":
                calculated = values[0] - sum(values[1:])
            elif operation in {"ratio", "percentage_change", "percentage_difference"}:
                if len(values) != 2:
                    raise FederationFailure("merge_input_invalid")
                if values[1] == 0:
                    raise FederationFailure("division_by_zero")
                calculated = values[0] / values[1]
                if operation in {"percentage_change", "percentage_difference"}:
                    calculated = (values[0] - values[1]) / values[1] * 100
            else:
                raise FederationFailure("unsupported_scalar_operation", blocked=True)
            row[operation] = _plain(calculated)
        return MergedResult(columns=list(row), rows=[row], row_count=1,
                            truncated=False, operation=plan.operation)

    @staticmethod
    def _union(inputs: list[AskResponse]) -> MergedResult:
        schemas = [response.result.columns for response in inputs if response.result]
        if not schemas or any(set(item) != set(schemas[0]) for item in schemas):
            raise FederationFailure("merge_schema_mismatch")
        columns = schemas[0]
        rows = [row for response in inputs for row in _rows(response)]
        if any(set(row) != set(columns) for row in rows):
            raise FederationFailure("merge_schema_mismatch")
        for column in columns:
            kinds = {type(row[column]) for row in rows if row.get(column) is not None}
            if len(kinds) > 1 and not kinds <= {int, float, Decimal}:
                raise FederationFailure("merge_schema_mismatch")
        return MergedResult(columns=columns, rows=rows, row_count=len(rows),
                            truncated=False, operation="union_rows")

    @staticmethod
    def _timeseries(inputs: list[AskResponse]) -> MergedResult:
        series: list[dict[str, Any]] = []
        for response in inputs:
            if not isinstance(response.plan, GroundedQueryPlan) or not response.plan.time_field:
                raise FederationFailure("merge_time_axis_unconfirmed")
            rows = _rows(response)
            axis = _column(rows, response.plan.time_field.field_path)
            if not rows:
                series.append({})
                continue
            metrics = [name for name in response.result.columns if name != axis]
            if len(metrics) != 1:
                raise FederationFailure("merge_input_invalid")
            values: dict[str, Any] = {}
            for row in rows:
                value = row.get(axis)
                if isinstance(value, (date, datetime)):
                    key = value.isoformat()
                elif isinstance(value, str) and value:
                    if not re.fullmatch(r"\d{4}-\d{2}", value):
                        try:
                            date.fromisoformat(value[:10])
                        except ValueError as error:
                            raise FederationFailure("merge_input_invalid") from error
                    elif not 1 <= int(value[-2:]) <= 12:
                        raise FederationFailure("merge_input_invalid")
                    key = value
                else:
                    raise FederationFailure("merge_input_invalid")
                if key in values:
                    raise FederationFailure("merge_time_axis_duplicate")
                values[key] = row.get(metrics[0])
            series.append(values)
        keys = sorted(set().union(*(set(item) for item in series)))
        columns = ["time_key"] + [f"source_{index + 1}_value" for index in range(len(inputs))]
        rows = [{"time_key": key, **{
            f"source_{index + 1}_value": item.get(key)
            for index, item in enumerate(series)
        }} for key in keys]
        return MergedResult(columns=columns, rows=rows, row_count=len(rows),
                            truncated=False, operation="align_time_series")

    @staticmethod
    def _join(inputs: list[AskResponse], plan: MergePlan, mapping: JoinMapping) -> MergedResult:
        left, right = inputs
        if not all(isinstance(item.plan, GroundedQueryPlan) for item in inputs):
            raise FederationFailure("join_projection_unconfirmed")
        if left.plan.datasource_id != mapping.left_datasource_id \
                or right.plan.datasource_id != mapping.right_datasource_id:
            raise FederationFailure("join_mapping_source_mismatch")
        for response, object_id, path in (
            (left, mapping.left_data_object_id, mapping.left_field_path),
            (right, mapping.right_data_object_id, mapping.right_field_path),
        ):
            if not any(field.data_object_id == object_id and field.field_path == path
                       for field in response.plan.selected_fields):
                raise FederationFailure("join_projection_unconfirmed")
        left_rows, right_rows = _rows(left), _rows(right)
        left_key = _column(left_rows, mapping.left_field_path)
        right_key = _column(right_rows, mapping.right_field_path)
        warnings: list[str] = []
        def prepare(rows: list[dict[str, Any]], key: str, side: str):
            nulls = [row for row in rows if row.get(key) is None]
            if nulls and mapping.null_policy == "REJECT":
                raise FederationFailure("join_null_key")
            if nulls:
                warnings.append(f"{side}_null_keys_dropped:{len(nulls)}")
            return [row for row in rows if row.get(key) is not None]
        left_rows = prepare(left_rows, left_key, "left")
        right_rows = prepare(right_rows, right_key, "right")
        for row in left_rows:
            try:
                hash(row[left_key])
            except TypeError as error:
                raise FederationFailure("merge_input_invalid") from error
        for row in right_rows:
            try:
                hash(row[right_key])
            except TypeError as error:
                raise FederationFailure("merge_input_invalid") from error
        for rows, key, unique in (
            (left_rows, left_key, mapping.cardinality in {"ONE_TO_ONE", "ONE_TO_MANY"}),
            (right_rows, right_key, mapping.cardinality in {"ONE_TO_ONE", "MANY_TO_ONE"}),
        ):
            if unique and any(count > 1 for count in Counter((type(row[key]), row[key])
                                                               for row in rows).values()):
                raise FederationFailure("join_cardinality_violation")
        right_index: dict[tuple[type, Any], list[dict[str, Any]]] = {}
        for row in right_rows:
            right_index.setdefault((type(row[right_key]), row[right_key]), []).append(row)
        left_columns = left.result.columns
        right_columns = right.result.columns
        columns = [f"left_{name}" for name in left_columns]
        if plan.join_mode != "anti":
            columns += [f"right_{name}" for name in right_columns]
        output: list[dict[str, Any]] = []
        for row in left_rows:
            matches = right_index.get((type(row[left_key]), row[left_key]), [])
            if plan.join_mode == "anti" and not matches:
                output.append({f"left_{name}": row.get(name) for name in left_columns})
            elif plan.join_mode == "left" and not matches:
                output.append({**{f"left_{name}": row.get(name) for name in left_columns},
                               **{f"right_{name}": None for name in right_columns}})
            elif plan.join_mode in {"inner", "left"}:
                for match in matches:
                    output.append({**{f"left_{name}": row.get(name) for name in left_columns},
                                   **{f"right_{name}": match.get(name) for name in right_columns}})
        return MergedResult(columns=columns, rows=output, row_count=len(output),
                            truncated=False, operation="keyed_join", warnings=warnings)
