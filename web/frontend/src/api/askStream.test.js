import test from "node:test";
import assert from "node:assert/strict";
import { streamAskQuestion } from "./askStream.js";

const originalFetch = globalThis.fetch;
test.afterEach(() => { globalThis.fetch = originalFetch; });
function sse(events, chunkSize = 7) {
  const bytes = new TextEncoder().encode(events.map((e) => `event: ${e.event_type}\ndata: ${JSON.stringify(e)}\n\n`).join(""));
  return new ReadableStream({ start(controller) { for (let i = 0; i < bytes.length; i += chunkSize) controller.enqueue(bytes.slice(i, i + chunkSize)); controller.close(); } });
}
const ev = (event_type, sequence, payload = {}) => ({ event_type, sequence, correlation_id: "corr-1", payload });

test("parses fragmented POST SSE and requires a genuine done event", async () => {
  const events = [ev("accepted", 1), ev("result_ready", 2, { response: { status: "completed" } }), ev("done", 3)]; const seen = [];
  globalThis.fetch = async (url, init) => { assert.equal(url, "/api/ask/stream"); assert.equal(init.method, "POST"); assert.deepEqual(JSON.parse(init.body), { question: "sales?", workspace_id: "default", datasource_id: "ds_1" }); return new Response(sse(events), { headers: { "content-type": "text/event-stream" } }); };
  await streamAskQuestion({ question: "sales?", datasourceId: "ds_1", onEvent: (e) => seen.push(e.event_type) }); assert.deepEqual(seen, ["accepted", "result_ready", "done"]);
});

test("rejects a stream without done and rejects event name mismatch", async () => {
  globalThis.fetch = async (_url, init) => { assert.deepEqual(JSON.parse(init.body), { question: "q", workspace_id: "default", datasource_id: null }); return new Response(sse([ev("accepted", 1)]), { headers: { "content-type": "text/event-stream" } }); };
  await assert.rejects(streamAskQuestion({ question: "q" }), { code: "stream_incomplete" });
  globalThis.fetch = async () => new Response(new ReadableStream({ start(c) { c.enqueue(new TextEncoder().encode(`event: nope\ndata: ${JSON.stringify(ev("accepted", 1))}\n\n`)); c.close(); } }), { headers: { "content-type": "text/event-stream" } });
  await assert.rejects(streamAskQuestion({ question: "q" }), { code: "invalid_event_stream" });
});
