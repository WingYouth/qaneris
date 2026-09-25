export const NETWORK_ERROR_CODE = "backend_unavailable";
export const INVALID_RESPONSE_CODE = "invalid_response";

export class ApiError extends Error {
  constructor(message, { status = 0, code = "request_failed", payload = null, cause } = {}) {
    super(message, cause ? { cause } : undefined);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.payload = payload;
  }
}

export function assertRelativeUrl(url) {
  if (typeof url !== "string" || !url.startsWith("/") || url.startsWith("//")) {
    throw new TypeError("Qaneris API requests must use a relative URL beginning with '/'.");
  }
}

export function parseResponseBody(text) {
  if (!text) {
    return { data: null, isJson: true };
  }

  try {
    return { data: JSON.parse(text), isJson: true };
  } catch {
    return { data: null, isJson: false };
  }
}

export function errorFromResponse(response, data, isJson) {
  const structuredError = isJson && data && typeof data.error === "object" ? data.error : null;
  const detail = isJson && typeof data?.detail === "string" ? data.detail : null;
  const message =
    structuredError?.message ||
    detail ||
    (isJson ? null : "The backend returned a non-JSON error response.") ||
    `Request failed with HTTP ${response.status}.`;
  const code = structuredError?.code || `http_${response.status}`;

  return new ApiError(message, {
    status: response.status,
    code,
    payload: isJson ? data : null,
  });
}

export async function requestJson(url, { json, headers, ...options } = {}) {
  assertRelativeUrl(url);

  const requestHeaders = new Headers(headers);
  const requestOptions = { ...options, headers: requestHeaders };

  if (json !== undefined) {
    requestHeaders.set("Content-Type", "application/json");
    requestOptions.body = JSON.stringify(json);
  }

  let response;
  try {
    response = await fetch(url, requestOptions);
  } catch (cause) {
    throw new ApiError("Unable to reach the Qaneris backend.", {
      status: 0,
      code: NETWORK_ERROR_CODE,
      cause,
    });
  }

  let text;
  try {
    text = await response.text();
  } catch (cause) {
    throw new ApiError("Unable to read the backend response.", {
      status: response.status,
      code: INVALID_RESPONSE_CODE,
      cause,
    });
  }

  const { data, isJson } = parseResponseBody(text);

  if (!response.ok) {
    throw errorFromResponse(response, data, isJson);
  }

  if (response.status === 204 || (response.status === 205 && !text)) {
    return null;
  }

  if (!isJson) {
    throw new ApiError("The backend returned an invalid JSON response.", {
      status: response.status,
      code: INVALID_RESPONSE_CODE,
    });
  }

  return data;
}

export function getJson(url, options) {
  return requestJson(url, { ...options, method: "GET" });
}

export function postJson(url, json, options) {
  return requestJson(url, { ...options, method: "POST", json });
}

export function putJson(url, json, options) {
  return requestJson(url, { ...options, method: "PUT", json });
}

export function deleteRequest(url, options) {
  return requestJson(url, { ...options, method: "DELETE" });
}

/**
 * Turn one completed multipart response into the parsed product payload.
 *
 * Uploads report progress through XHR rather than fetch, so they cannot share ``requestJson``. The
 * status handling and the structured-error contract are still the same one, which is why the body
 * parsing and the error mapping are shared instead of re-derived here.
 */
export function resolveUploadResponse({ status, responseText, url }) {
  const ok = status >= 200 && status < 300;
  const { data: payload, isJson } = parseResponseBody(responseText);

  if (!ok) {
    throw errorFromResponse({ status }, payload, isJson);
  }
  if (!isJson) {
    throw new ApiError("The backend returned an invalid JSON response.", {
      status,
      code: INVALID_RESPONSE_CODE,
      payload: { url },
    });
  }
  return payload;
}
