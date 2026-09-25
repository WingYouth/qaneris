import { ApiError, getJson } from "./client.js";

export async function checkBackendHealth() {
  try {
    const health = await getJson("/health");

    if (health?.status !== "ok") {
      throw new ApiError("The backend health response was not healthy.", {
        status: 200,
        code: "unhealthy_response",
        payload: health,
      });
    }

    return {
      availability: "available",
      version: health.version || null,
      error: null,
    };
  } catch (error) {
    const apiError =
      error instanceof ApiError
        ? error
        : new ApiError("Unable to check backend health.", {
            code: "health_check_failed",
            cause: error,
          });

    return {
      availability: "unavailable",
      version: null,
      error: apiError,
    };
  }
}
