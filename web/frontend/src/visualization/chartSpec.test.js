import assert from "node:assert/strict";
import { test } from "node:test";
import { buildChartSpec, isChartSpec } from "./chartSpec.js";
import { linearScale, lineSegments, numericDomain, pieSlices } from "./chartMath.js";

const rows = [{ region: "East", sum_amount: 350.5 }, { region: "West", sum_amount: 220.25 }];
function view(overrides = {}) {
  return { status: "completed", table: { columns: ["region", "sum_amount"], rows, rowCount: 2, truncated: false },
    plan: { groupBy: ["region"], aggregates: [{ alias: "sum_amount" }] },
    evidence: { datasourceId: "ds_xxx", scanVersion: 1 }, ...overrides };
}

test("spec projects only controlled fields, values and source metadata deterministically", () => {
  const spec = buildChartSpec(view(), "bar");
  assert.deepEqual(spec, buildChartSpec(view(), "bar"));
  assert.equal(spec.kind, "bar");
  assert.equal(spec.category.field, "region");
  assert.equal(spec.series[0].field, "sum_amount");
  assert.deepEqual(spec.points, [
    { category: "East", values: { sum_amount: 350.5 } },
    { category: "West", values: { sum_amount: 220.25 } },
  ]);
  assert.deepEqual(spec.source, { rowCount: 2, truncated: false, datasourceId: "ds_xxx", scanVersion: 1 });
  assert.equal(buildChartSpec(view(), "table"), null);
});

test("injected category stays a plain text value and backend extras are excluded", () => {
  const malicious = "<script>alert(1)</script>";
  const spec = buildChartSpec(view({ table: { columns: ["region", "sum_amount"],
    rows: [{ region: malicious, sum_amount: 1 }, rows[1]], rowCount: 2, truncated: true },
  evidence: { datasourceId: "ds", scanVersion: 1, displayCommand: "SELECT secret", onclick: "evil" } }), "bar");
  assert.equal(spec.points[0].category, malicious);
  assert.equal(spec.source.truncated, true);
  assert.doesNotMatch(JSON.stringify(spec), /SELECT secret|onclick/);
});

test("missing numeric values remain missing in chart points", () => {
  const spec = buildChartSpec(view({ table: { columns: ["region", "sum_amount"],
    rows: [{ region: "East", sum_amount: null }, rows[1]], rowCount: 2 } }), "bar");
  assert.equal(spec.points[0].values.sum_amount, null);
});

test("truncated results chart the returned rows and retain the warning flag", () => {
  const spec = buildChartSpec(view({ table: { columns: ["region", "sum_amount"],
    rows, rowCount: 100, truncated: true } }), "bar");
  assert.equal(spec.points.length, 2);
  assert.equal(spec.source.truncated, true);
});

test("bar domains and scale handle positive, negative, mixed and constant values", () => {
  assert.deepEqual(numericDomain([2, 5], true), [0, 5]);
  assert.deepEqual(numericDomain([-5, -2], true), [-5, 0]);
  assert.deepEqual(numericDomain([-5, 2], true), [-5, 2]);
  const domain = numericDomain([10, 10, 10]);
  assert.ok(domain[0] < 10 && domain[1] > 10);
  assert.ok(Number.isFinite(linearScale(domain, [200, 0])(10)));
});

test("pie slice geometry covers full turn and refuses zero or negative values", () => {
  const slices = pieSlices([1, 2, 3]);
  assert.ok(Math.abs(slices.at(-1).end - 2 * Math.PI) < 1e-10);
  assert.equal(pieSlices([0, 0]), null);
  assert.equal(pieSlices([1, -1]), null);
});

test("a missing line value creates a gap without turning it into zero", () => {
  const points = [1, null, 2, 3].map((value, index) => ({ category: String(index), values: { amount: value } }));
  assert.deepEqual(lineSegments(points, "amount").map((group) => group.map((point) => point.index)), [[0], [2, 3]]);
});

test("unsupported and malformed chart specifications safely fall back", () => {
  const valid = buildChartSpec(view(), "bar");
  assert.equal(buildChartSpec(view(), "scatter"), null);
  assert.equal(isChartSpec({ ...valid, kind: "scatter" }), false);
  assert.equal(isChartSpec({ ...valid, points: [{ category: "East", values: { sum_amount: Infinity } }] }), false);
  assert.equal(isChartSpec({ ...valid, series: [] }), false);
  assert.equal(isChartSpec(null), false);
});
