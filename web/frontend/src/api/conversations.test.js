import test from "node:test";
import assert from "node:assert/strict";
import { cancelRun, clarifyRun, createConversation, createRun, getConversation, getRun, listConversations, retryRun } from "./conversations.js";

const originalFetch = globalThis.fetch;
test.afterEach(() => { globalThis.fetch = originalFetch; });
test("conversation and run clients use only relative URLs and the contracted bodies", async () => {
  const calls = [];
  globalThis.fetch = async (url, options) => { calls.push({ url, method: options.method, body: options.body && JSON.parse(options.body) }); return new Response("{}", { headers: { "content-type": "application/json" } }); };
  await createConversation({ workspaceId: "ws", datasourceIds: ["one", "two"] });
  await listConversations("ws"); await getConversation("conv/1");
  await createRun("conv/1", { question: "sales", clientRequestId: "stable" });
  await getRun("run/1"); await clarifyRun("run/1", "option"); await cancelRun("run/1"); await retryRun("run/1");
  assert.deepEqual(calls.map((call) => [call.method, call.url]), [
    ["POST", "/api/conversations"], ["GET", "/api/conversations?workspace_id=ws"],
    ["GET", "/api/conversations/conv%2F1"], ["POST", "/api/conversations/conv%2F1/runs"],
    ["GET", "/api/runs/run%2F1"], ["POST", "/api/runs/run%2F1/clarification"],
    ["POST", "/api/runs/run%2F1/cancel"], ["POST", "/api/runs/run%2F1/retry"],
  ]);
  assert.deepEqual(calls[0].body, { workspace_id: "ws", datasource_ids: ["one", "two"], title: "" });
  assert.deepEqual(calls[3].body, { question: "sales", max_rows: 200, client_request_id: "stable" });
  assert.deepEqual(calls[5].body, { answer: "option" });
  assert(calls.every((call) => call.url.startsWith("/api/") && !call.url.startsWith("//")));
});
