import { ApiError } from "./client.js";

/** Read SSE frames once; callers validate their own event contract. */
export async function parseSSEStream(response, onFrame) {
  if (!response.headers.get("content-type")?.toLowerCase().startsWith("text/event-stream"))
    throw new ApiError("Stream returned the wrong content type.", { code: "invalid_event_stream", status: response.status });
  if (!response.body?.getReader) throw new ApiError("Streaming is unavailable in this browser.", { code: "invalid_event_stream" });
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const consume = (frame) => {
    let eventName = "";
    const dataLines = [];
    for (const line of frame.split(/\r?\n/)) {
      if (line.startsWith("event:")) eventName = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
    }
    if (!dataLines.length) return;
    let data;
    try { data = JSON.parse(dataLines.join("\n")); }
    catch (cause) { throw new ApiError("Stream contained invalid JSON.", { code: "invalid_event_stream", cause }); }
    onFrame({ eventName, data });
  };
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      let split;
      while ((split = buffer.search(/\r?\n\r?\n/)) >= 0) {
        const frame = buffer.slice(0, split);
        const delimiter = buffer.slice(split).match(/^\r?\n\r?\n/)[0];
        buffer = buffer.slice(split + delimiter.length);
        consume(frame);
      }
      if (done) break;
    }
    if (buffer.trim()) consume(buffer);
  } finally { reader.releaseLock(); }
}
