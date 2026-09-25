import assert from "node:assert/strict";
import { test } from "node:test";

import {
  ASK_PHASE,
  askCompleted,
  askFailed,
  askStarted,
  askViewFromResponse,
  initialAskState,
} from "./askView.js";

const COMPLETED = {
  question: "对 orders 表，按 region 分组汇总 amount 的总和。",
  status: "completed",
  answer: "已完成，返回 2 行数据。",
  plan: {
    plan_id: "plan_09cdd6eaafbc",
    datasource_id: "ds_1",
    data_object_ids: ["obj_1"],
    data_objects: { obj_1: { datasource_id: "ds_1", data_object_id: "obj_1", name: "orders" } },
    scan_version: 1,
    aggregates: [{ function: "sum", field: { field_path: "amount" }, alias: "sum_amount" }],
    group_by: [{ field_path: "region" }],
    time_field: { field_path: "created_at" },
    filters: [],
    expected_result_type: "table",
  },
  result: {
    plan_id: "plan_09cdd6eaafbc",
    datasource_id: "ds_1",
    query_language: "sql",
    columns: ["region", "sum_amount"],
    rows: [
      { region: "East", sum_amount: 350.5 },
      { region: "West", sum_amount: 220.25 },
    ],
    row_count: 2,
    truncated: false,
    scan_version: 1,
  },
  evidence: {
    plan_id: "plan_09cdd6eaafbc",
    datasource_id: "ds_1",
    scan_version: 1,
    query_language: "sql",
    display_command: 'SELECT t0."region", SUM(t0."amount") AS "sum_amount" FROM "orders" AS t0 GROUP BY t0."region"',
    row_count: 2,
    truncated: false,
  },
};

test("a completed answer renders the typed table, the plan and the evidence", () => {
  const view = askViewFromResponse(COMPLETED);

  assert.equal(view.status, "completed");
  assert.deepEqual(view.table.columns, ["region", "sum_amount"]);
  assert.equal(view.table.rowCount, 2);
  assert.equal(view.table.truncated, false);
  assert.deepEqual(view.table.rows[0], { region: "East", sum_amount: 350.5 });
  assert.equal(view.plan.kind, "grounded");
  assert.deepEqual(view.plan.aggregates, [{ function: "sum", field: "amount", alias: "sum_amount" }]);
  assert.deepEqual(view.plan.groupBy, ["region"]);
  assert.equal(view.plan.timeField, "created_at");
  assert.deepEqual(view.plan.objects, ["orders"]);
  assert.match(view.evidence.displayCommand, /GROUP BY/);
  assert.equal(view.evidence.rowCount, 2);
});

test("a clarification is shown as the backend asked it, with no invented option", () => {
  const view = askViewFromResponse({
    question: "销售额是多少？",
    status: "clarification_required",
    answer: "请确认指标",
    clarification: [{ question: "请确认：未找到与指标“销售额”匹配的语义候选", options: [] }],
  });

  assert.equal(view.status, "clarification_required");
  assert.equal(view.table, null);
  assert.equal(view.clarification.length, 1);
  assert.match(view.clarification[0].question, /未找到与指标/);
  assert.deepEqual(view.clarification[0].options, []);
});

test("a failed answer keeps its structured error", () => {
  const view = askViewFromResponse({
    question: "q",
    status: "failed",
    error: { code: "query_planning_failed", message: "无法规划" },
  });

  assert.equal(view.status, "failed");
  assert.equal(view.error.code, "query_planning_failed");
});

test("a legacy explicit-SQL plan is represented without pretending it is grounded", () => {
  const view = askViewFromResponse({
    question: "q",
    status: "completed",
    plan: [{ id: "legacy-explicit-sql", query_language: "sql" }],
    result: { source: "ds", dataset: "query", columns: [], rows: [], row_count: 0 },
  });

  assert.equal(view.plan.kind, "legacy");
  assert.equal(view.plan.timeField, undefined);
  assert.equal(view.table.rowCount, 0);
});

test("an unusable response payload does not produce a view", () => {
  assert.equal(askViewFromResponse(null), null);
  assert.equal(askViewFromResponse("completed"), null);
});

test("the ask phase follows the backend status, and transport errors stay separate", () => {
  const asked = askStarted(initialAskState(), "q");
  assert.equal(asked.phase, ASK_PHASE.ASKING);

  const completed = askCompleted(asked, COMPLETED);
  assert.equal(completed.phase, ASK_PHASE.COMPLETED);
  assert.equal(completed.response.table.rowCount, 2);

  const clarification = askCompleted(asked, { status: "clarification_required" });
  assert.equal(clarification.phase, "clarification_required");

  const errored = askFailed(asked, { code: "backend_unavailable", message: "无法连接" });
  assert.equal(errored.phase, ASK_PHASE.ERROR);
  assert.equal(errored.response, null);
});
