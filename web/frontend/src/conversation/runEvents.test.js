import test from "node:test";
import assert from "node:assert/strict";
import { runEventView, shouldRefreshRun } from "./runEvents.js";

test("only whitelisted event labels and bounded public details are projected", () => {
  assert.equal(runEventView({ sequence: 1, event_type: "ASK_PROGRESS", public_payload: { ask_event_type: "plan_ready", password: "secret" } }).detail, "查询计划已确认");
  assert.equal(runEventView({ sequence: 2, event_type: "EVIDENCE_QUESTION_PLANNED", public_payload: { question: "按地区?" } }).detail, "按地区?");
  assert.equal(runEventView({ sequence: 3, event_type: "PRIVATE_REASONING", public_payload: { prompt: "secret" } }), null);
  assert.equal(shouldRefreshRun({ event_type: "SOURCE_TASK_COMPLETED" }), true);
  assert.equal(shouldRefreshRun({ event_type: "ASK_PROGRESS", public_payload: { ask_event_type: "retrieval_ready" } }), false);
});
