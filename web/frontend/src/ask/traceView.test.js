import test from "node:test";
import assert from "node:assert/strict";
import { traceItem } from "./traceView.js";

test("trace projects only safe stage summaries and the public display command", () => {
  const item = traceItem({ event_type: "query_ready", sequence: 4, payload: { display_command: "SELECT 1", parameters: { password: "do-not-show" } } });
  assert.equal(item.summary, "SELECT 1"); assert.equal(JSON.stringify(item).includes("do-not-show"), false); assert.equal(traceItem({ event_type: "unknown", sequence: 5, payload: { dump: "private" } }), null);
});
