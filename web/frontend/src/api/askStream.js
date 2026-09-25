import { ApiError, assertRelativeUrl, errorFromResponse, parseResponseBody } from "./client.js";
import { parseSSEStream } from "./sse.js";

export async function streamAskQuestion({ question, workspaceId = "default", datasourceId, onEvent, signal }) {
  const url = "/api/ask/stream"; assertRelativeUrl(url);
  let response;
  try {
    response = await fetch(url, { method: "POST", signal, headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify({ question, workspace_id: workspaceId, datasource_id: datasourceId || null }) });
  } catch (cause) { throw new ApiError("Unable to reach the SmartData backend.", { code: "backend_unavailable", cause }); }
  if (!response.ok) {
    const { data, isJson } = parseResponseBody(await response.text());
    throw errorFromResponse(response, data, isJson);
  }
  let previous = 0; let done = false;
  await parseSSEStream(response, ({ eventName, data: event }) => {
    if (typeof event.event_type !== "string" || !Number.isInteger(event.sequence) || event.sequence < 1 || !event.payload || Array.isArray(event.payload) || typeof event.payload !== "object" || typeof event.correlation_id !== "string" || !event.correlation_id || eventName !== event.event_type || event.sequence <= previous) throw new ApiError("Ask stream contract is invalid.", { code: "invalid_event_stream" });
    previous = event.sequence; if (event.event_type === "done") done = true; onEvent?.(event);
  });
  if (!done) throw new ApiError("Ask stream ended before its done event.", { code: "stream_incomplete" });
}
