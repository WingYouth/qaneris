import test from "node:test";
import assert from "node:assert/strict";
import { conversationReducer, initialConversationState, latestRunId, messageTurns } from "./conversationState.js";

test("messages and results stay keyed to their own run through refresh", () => {
  let state = initialConversationState();
  state = conversationReducer(state, { type: "open", detail: { conversation: { conversation_id: "c" }, messages: [] } });
  for (const id of ["r1", "r2"]) {
    state = conversationReducer(state, { type: "message", message: { message_id: `m${id}`, role: "user", run_id: id, content: id } });
    state = conversationReducer(state, { type: "run", run: { run_id: id, response_json: { answer: id } } });
  }
  state = conversationReducer(state, { type: "refresh", detail: { conversation: { conversation_id: "c" }, messages: state.messages } });
  assert.equal(state.runs.r1.response_json.answer, "r1"); assert.equal(state.runs.r2.response_json.answer, "r2");
  assert.equal(latestRunId(state.messages), "r2"); assert.equal(messageTurns(state.messages).length, 2);
});

test("reopen replaces conversation while replay deduplicates sequence", () => {
  let state = conversationReducer(initialConversationState(), { type: "open", detail: { conversation: { conversation_id: "c" }, messages: [{ role: "user", run_id: "r" }] } });
  state = conversationReducer(state, { type: "event", event: { run_id: "r", sequence: 1 } });
  state = conversationReducer(state, { type: "event", event: { run_id: "r", sequence: 1 } });
  assert.equal(state.events.r.length, 1);
  state = conversationReducer(state, { type: "open", detail: { conversation: { conversation_id: "d" }, messages: [] } });
  assert.deepEqual(state.runs, {});
});
