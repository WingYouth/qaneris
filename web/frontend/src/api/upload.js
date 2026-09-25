import {
  ApiError,
  INVALID_RESPONSE_CODE,
  NETWORK_ERROR_CODE,
  resolveUploadResponse,
} from "./client.js";

export const UPLOAD_ABORTED_CODE = "upload_aborted";

/**
 * The browser transport: XMLHttpRequest, because it is the only API that reports how many bytes of
 * the request body have actually been sent.
 *
 * The progress it reports is real upload progress. Nothing here invents a stage the backend did not
 * announce, and there is no simulated percentage: a browser that cannot report byte counts simply
 * reports no number.
 */
export function uploadFormData(url, formData, { onUploadProgress, onUploadComplete } = {}) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", url, true);

    if (typeof onUploadProgress === "function") {
      request.upload.addEventListener("progress", (event) => {
        if (event.lengthComputable) {
          onUploadProgress({ loaded: event.loaded, total: event.total });
        }
      });
    }

    if (typeof onUploadComplete === "function") {
      // Every request byte has been handed over. The server is importing from here on, which is a
      // different phase from still sending bytes - and one the browser can observe for real.
      request.upload.addEventListener("load", () => {
        onUploadComplete();
      });
    }

    request.addEventListener("load", () => {
      try {
        resolve(resolveUploadResponse({ status: request.status, responseText: request.responseText, url }));
      } catch (error) {
        reject(error);
      }
    });

    request.addEventListener("error", () => {
      reject(
        new ApiError("Unable to reach the SmartData backend.", {
          status: 0,
          code: NETWORK_ERROR_CODE,
        }),
      );
    });

    request.addEventListener("abort", () => {
      reject(new ApiError("The upload was cancelled.", { status: 0, code: UPLOAD_ABORTED_CODE }));
    });

    // The multipart boundary is set by the browser; no Content-Type may be set by hand here.
    request.send(formData);
  });
}

/**
 * A transport built on ``fetch``, for runtimes without XMLHttpRequest.
 *
 * It sends exactly the same request as the browser transport - same URL, same multipart body, same
 * response and error handling - but it cannot report byte progress. No fake progress is emitted to
 * paper over that.
 */
export function createFetchTransport(fetchImpl, { onUploadComplete } = {}) {
  return async function fetchFormData(url, formData) {
    if (typeof onUploadComplete === "function") {
      // fetch cannot report byte progress; it can still say that the body is on its way out.
      onUploadComplete();
    }
    let response;
    try {
      response = await fetchImpl(url, { method: "POST", body: formData });
    } catch (cause) {
      throw new ApiError("Unable to reach the SmartData backend.", {
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

    return resolveUploadResponse({ status: response.status, responseText: text, url });
  };
}
