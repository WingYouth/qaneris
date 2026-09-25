import test from "node:test";
import assert from "node:assert/strict";
import { subscribeRunEvents } from "./runStream.js";

const originalFetch = globalThis.fetch;
test.afterEach(() => { globalThis.fetch = originalFetch; });
const event = (sequence, event_type = "RUN_STARTED") => ({ run_id: "r", sequence, event_type, public_payload: {} });
const response = (items, contentType = "text/event-stream") => new Response(new ReadableStream({ start(controller) {
  const bytes = new TextEncoder().encode(items.map((item) => `id: ${item.sequence}\nevent: ${item.event_type}\ndata: ${JSON.stringify(item)}\n\n`).join(""));
  for (let i = 0; i < bytes.length; i += 3) controller.enqueue(bytes.slice(i, i + 3));
  controller.close();
} }), { headers: { "content-type": contentType } });

test("reconnects with last sequence and does not create a second run", async () => {
  const urls = []; const seen = [];
  globalThis.fetch = async (url, options) => { urls.push(url); assert.equal(options.method, "GET"); return urls.length === 1 ? response([event(1)]) : response([event(2, "RUN_COMPLETED")]); };
  const last = await subscribeRunEvents({ runId: "r", onEvent: (e) => seen.push(e.sequence), reconnectDelay: 0 });
  assert.equal(last, 2); assert.deepEqual(seen, [1, 2]);
  assert.deepEqual(urls, ["/api/runs/r/stream?after_sequence=0", "/api/runs/r/stream?after_sequence=1"]);
});

test("starts from requested sequence and accepts each settled outcome", async () => {
  for (const terminal of ["RUN_COMPLETED", "RUN_FAILED", "RUN_BLOCKED", "RUN_CANCELLED", "CLARIFICATION_REQUIRED"]) {
    globalThis.fetch = async (url) => { assert.equal(url, "/api/runs/r/stream?after_sequence=4"); return response([event(5, terminal)]); };
    assert.equal(await subscribeRunEvents({ runId: "r", afterSequence: 4, maxReconnects: 0 }), 5);
  }
});

test("rejects duplicate, out-of-order, malformed JSON, and wrong content type", async () => {
  for (const items of [[event(1), event(1)], [event(2), event(1)]]) {
    globalThis.fetch = async () => response(items);
    await assert.rejects(subscribeRunEvents({ runId: "r", maxReconnects: 0 }), { code: "invalid_event_stream" });
  }
  globalThis.fetch = async () => new Response("event: X\ndata: {bad}\n\n", { headers: { "content-type": "text/event-stream" } });
  await assert.rejects(subscribeRunEvents({ runId: "r", maxReconnects: 0 }), { code: "invalid_event_stream" });
  globalThis.fetch = async () => response([], "application/json");
  await assert.rejects(subscribeRunEvents({ runId: "r", maxReconnects: 0 }), { code: "invalid_event_stream" });
});
