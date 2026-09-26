import test from "node:test";
import assert from "node:assert/strict";
import {
  confirmFailureMessage,
  createFailureMessage,
  datasourcePairs,
  fieldsFor,
  mappingSummary,
  needingAttention,
  objectsFor,
  suggestedKeys,
} from "./joinMappingView.js";

const objects = [
  { node_id: "obj_a", datasource_id: "orders", datasource_name: "订单库", name: "orders",
    fields: [{ node_id: "f1", path: "customer_id" }, { node_id: "f2", path: "amount" }] },
  { node_id: "obj_b", datasource_id: "crm", datasource_name: "客户库", name: "customers",
    fields: [{ node_id: "f3", path: "id" }, { node_id: "f4", path: "name" }] },
];

test("pairs are built from distinct datasources only", () => {
  const pairs = datasourcePairs(objects);
  assert.equal(pairs.length, 1);
  assert.deepEqual(pairs[0].left, { id: "crm", name: "客户库" });
  assert.deepEqual(pairs[0].right, { id: "orders", name: "订单库" });
});

test("a single datasource offers no pair, matching the backend refusal", () => {
  assert.deepEqual(datasourcePairs([objects[0]]), []);
});

test("objects and fields resolve within one side", () => {
  assert.equal(objectsFor(objects, "orders").length, 1);
  assert.deepEqual(fieldsFor(objects, "orders", "obj_a").map((f) => f.path), ["customer_id", "amount"]);
  assert.deepEqual(fieldsFor(objects, "orders", "missing"), []);
});

test("a foreign key is suggested against the primary key it names", () => {
  const left = [{ node_id: "a", path: "customer_id" }];
  const right = [{ node_id: "b", path: "id" }];
  const suggestions = suggestedKeys(left, right);
  assert.equal(suggestions.length, 1);
  assert.equal(suggestions[0].left.path, "customer_id");
  assert.equal(suggestions[0].right.path, "id");
});

test("a shared prefix is not treated as the same key", () => {
  assert.deepEqual(suggestedKeys([{ node_id: "a", path: "customer_id" }], [{ node_id: "b", path: "customer_code" }]), []);
});

test("unrelated field names produce no suggestion", () => {
  assert.deepEqual(suggestedKeys([{ node_id: "a", path: "amount" }], [{ node_id: "b", path: "region" }]), []);
});

test("only a confirmed mapping is treated as settled", () => {
  const rows = [{ mapping_id: "1", status: "CONFIRMED" }, { mapping_id: "2", status: "CANDIDATE" },
    { mapping_id: "3", status: "STALE" }, { mapping_id: "4", status: "REJECTED" }];
  assert.deepEqual(needingAttention(rows).map((r) => r.mapping_id), ["2", "3", "4"]);
});

test("a graph failure names the graph, not a generic error", () => {
  const message = createFailureMessage({ code: "graph_unavailable", message: "图后端不可用" });
  assert.match(message, /Neo4j/);
});

test("an unavailable backend is distinguished from a rejected mapping", () => {
  assert.match(confirmFailureMessage({ code: "backend_unavailable" }), /后端/);
  assert.match(confirmFailureMessage({ code: "invalid_request", message: "字段不存在" }), /字段不存在/);
});

test("the mapping row shows both field paths", () => {
  assert.equal(mappingSummary({ left_field_path: "customer_id", right_field_path: "id" }), "customer_id → id");
});
