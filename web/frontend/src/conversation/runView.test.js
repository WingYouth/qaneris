import test from "node:test";
import assert from "node:assert/strict";
import { runFailure, runView } from "./runView.js";

test("projects normal and diagnostic responses by explicit run kind", () => {
  const normal = runView({ run_kind: "normal", status: "COMPLETED", response_json: { status: "completed", answer: "42", result: { columns: ["value"], rows: [{ value: 42 }] } } });
  assert.equal(normal.kind, "normal"); assert.equal(normal.visualization.table.rows[0].value, 42);
  const diagnostic = runView({ run_kind: "diagnostic", status: "COMPLETED", response_json: { status: "completed", answer: "下降来自华东" } });
  assert.equal(diagnostic.kind, "diagnostic"); assert.equal(diagnostic.answer, "下降来自华东");
});
test("federated merged table uses existing visualization model and safe evidence", () => {
  const view = runView({ run_kind: "federated", status: "COMPLETED", source_tasks: [{ task_id: "a", status: "COMPLETED" }], response_json: { answer: "done", merged_result: { columns: ["name"], rows: [{ name: "A" }], row_count: 1 }, federated_evidence: { merge_operation: "compare_scalars" } } });
  assert.equal(view.visualization.table.rows[0].name, "A"); assert.equal(view.sourceTasks[0].task_id, "a"); assert.equal(view.evidence.merge_operation, "compare_scalars");
  assert.match(runFailure({ failure_code: "unconfirmed_mapping" }), /已确认/);
});
