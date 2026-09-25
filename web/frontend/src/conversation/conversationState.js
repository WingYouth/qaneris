export const RUN_TERMINAL = new Set(["COMPLETED", "FAILED", "CANCELLED", "BLOCKED"]);
export const RUN_BUSY = new Set(["CREATED", "CONTEXTUALIZING", "DISCOVERING", "PLANNING", "EXECUTING", "ANSWERING", "WAITING_USER"]);

export function initialConversationState() {
  return { conversation: null, messages: [], runs: {}, events: {}, sequences: {} };
}

export function conversationReducer(state, action) {
  switch (action.type) {
    case "open": return { ...initialConversationState(), conversation: action.detail.conversation, messages: action.detail.messages || [] };
    case "refresh": return { ...state, conversation: action.detail.conversation, messages: action.detail.messages || [] };
    case "message": return { ...state, messages: state.messages.some((m) => m.message_id === action.message.message_id)
      ? state.messages : [...state.messages, action.message] };
    case "run": {
      const previous = state.runs[action.run.run_id];
      if (previous?.revision != null && action.run.revision != null && action.run.revision < previous.revision) return state;
      return { ...state, runs: { ...state.runs, [action.run.run_id]: { ...previous, ...action.run } } };
    }
    case "event": {
      const event = action.event;
      const previous = state.sequences[event.run_id] || 0;
      if (event.sequence <= previous) return state;
      return { ...state, sequences: { ...state.sequences, [event.run_id]: event.sequence },
        events: { ...state.events, [event.run_id]: [...(state.events[event.run_id] || []), event] } };
    }
    default: return state;
  }
}

export function runIds(messages) { return [...new Set(messages.map((m) => m.run_id).filter(Boolean))]; }
export function latestRunId(messages) { return runIds(messages).at(-1) || null; }
export function conversationTitle(conversation, messages = []) {
  return conversation?.title || messages.find((m) => m.role === "user" && m.message_kind === "normal")?.content.slice(0, 28) || "新对话";
}

export function messageTurns(messages) {
  const turns = [];
  const byRun = new Map();
  for (const message of messages) {
    if (!message.run_id) continue;
    let turn = byRun.get(message.run_id);
    if (!turn) { turn = { runId: message.run_id, messages: [] }; byRun.set(message.run_id, turn); turns.push(turn); }
    turn.messages.push(message);
  }
  return turns;
}
