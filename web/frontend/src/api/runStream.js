import { ApiError, assertRelativeUrl, errorFromResponse, parseResponseBody } from "./client.js";
import { parseSSEStream } from "./sse.js";

export const SETTLED_EVENTS = new Set(["RUN_COMPLETED", "RUN_FAILED", "RUN_BLOCKED", "RUN_CANCELLED", "CLARIFICATION_REQUIRED"]);
const pause = (ms, signal) => new Promise((resolve, reject) => {
  const abort = () => { clearTimeout(timer); reject(signal.reason || new DOMException("Aborted", "AbortError")); };
  const timer = setTimeout(() => { signal?.removeEventListener("abort", abort); resolve(); }, ms);
  if (signal?.aborted) abort();
  else signal?.addEventListener("abort", abort, { once: true });
});

/** A GET-only observer. Reconnects from the last accepted persisted sequence. */
export async function subscribeRunEvents({ runId, afterSequence = 0, onEvent, signal, reconnectDelay = 300, maxReconnects = Infinity }) {
  let cursor = afterSequence;
  let reconnects = 0;
  while (!signal?.aborted) {
    const url = `/api/runs/${encodeURIComponent(runId)}/stream?after_sequence=${cursor}`;
    assertRelativeUrl(url);
    let response;
    try { response = await fetch(url, { method: "GET", headers: { Accept: "text/event-stream" }, signal }); }
    catch (cause) {
      if (signal?.aborted) throw cause;
      if (reconnects++ >= maxReconnects) throw new ApiError("Run stream disconnected.", { code: "stream_incomplete", cause });
      await pause(reconnectDelay, signal);
      continue;
    }
    if (!response.ok) {
      const { data, isJson } = parseResponseBody(await response.text());
      throw errorFromResponse(response, data, isJson);
    }
    let settled = false;
    try {
      await parseSSEStream(response, ({ eventName, data }) => {
        if (!data || data.run_id !== runId || data.event_type !== eventName || !Number.isInteger(data.sequence) || data.sequence <= cursor || !data.public_payload || Array.isArray(data.public_payload) || typeof data.public_payload !== "object")
          throw new ApiError("Run stream contract is invalid.", { code: "invalid_event_stream" });
        cursor = data.sequence;
        onEvent?.(data);
        settled = SETTLED_EVENTS.has(data.event_type);
      });
    } catch (error) {
      if (error.code === "invalid_event_stream" || signal?.aborted) throw error;
      if (reconnects++ >= maxReconnects) throw error;
      await pause(reconnectDelay, signal);
      continue;
    }
    if (settled) return cursor;
    if (reconnects++ >= maxReconnects) throw new ApiError("Run stream ended before a settled event.", { code: "stream_incomplete" });
    await pause(reconnectDelay, signal);
  }
  return cursor;
}
