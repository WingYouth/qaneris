import assert from "node:assert/strict";
import { test } from "node:test";
import { analyzeTable, automaticChartKind, eligibleChartKinds } from "./chartPolicy.js";

function view(rows, columns = ["region", "amount"], plan = {}) {
  return { status: "completed", table: { columns, rows, rowCount: rows.length, truncated: false }, plan };
}

test("numeric fields require finite numbers in every non-null row", () => {
  for (const [values, numeric] of [
    [[1, 2], true], [[null, 2], true], [["1", "2"], false],
    [[Infinity, 2], false], [[NaN, 2], false], [[1, "2"], false],
  ]) {
    const result = analyzeTable(view(values.map((amount, i) => ({ region: `r${i}`, amount }))).table, {});
    assert.equal(result.numeric.includes("amount"), numeric);
  }
});

test("automatic order uses explicit time field, then category, then table", () => {
  const rows = [{ region: "East", amount: 3 }, { region: "West", amount: 2 }];
  assert.equal(automaticChartKind(view(rows, undefined, { timeField: "region" })), "line");
  assert.equal(automaticChartKind(view(rows)), "bar");
  assert.equal(automaticChartKind(view([{ amount: 3 }], ["amount"])), "table");
  assert.equal(automaticChartKind(view(rows.map(({ region, amount }) => ({ region, amount: String(amount) })))), "table");
  assert.equal(automaticChartKind(view(Array.from({ length: 51 }, (_, i) => ({ region: `r${i}`, amount: i })))), "table");
  assert.equal(automaticChartKind(view(rows, undefined, { timeField: "missing" })), "bar");
});

test("pie requires one complete nonnegative numeric series, 2-8 rows and a positive total", () => {
  const rows = [{ region: "East", amount: 3 }, { region: "West", amount: 2 }];
  assert.ok(eligibleChartKinds(view(rows)).includes("pie"));
  for (const rejected of [
    [{ region: "East", amount: -1 }, rows[1]],
    rows.map((row) => ({ ...row, amount: 0 })),
    [...rows, ...Array.from({ length: 7 }, (_, i) => ({ region: `r${i}`, amount: i }))],
    [{ region: "East", amount: null }, rows[1]],
  ]) assert.ok(!eligibleChartKinds(view(rejected)).includes("pie"));
  assert.ok(!eligibleChartKinds(view(rows.map((row) => ({ ...row, other: 2 })),
    ["region", "amount", "other"])).includes("pie"));
});

test("group by and aggregate aliases determine category and at most three series", () => {
  const rows = [{ region: "East", note: "A", a: 1, b: 2, c: 3, d: 4 },
    { region: "West", note: "B", a: 2, b: 3, c: 4, d: 5 }];
  const analysis = analyzeTable(view(rows, ["note", "region", "a", "b", "c", "d"],
    { groupBy: ["region"], aggregates: [{ alias: "d" }] }).table,
  { groupBy: ["region"], aggregates: [{ alias: "d" }] });
  assert.equal(analysis.category, "region");
  assert.deepEqual(analysis.series, ["d", "a", "b"]);
});

test("non-completed and non-tabular results only allow table", () => {
  assert.deepEqual(eligibleChartKinds({ status: "clarification_required" }), ["table"]);
  assert.deepEqual(eligibleChartKinds({ status: "failed" }), ["table"]);
  assert.deepEqual(eligibleChartKinds({ status: "completed", table: null }), ["table"]);
});
