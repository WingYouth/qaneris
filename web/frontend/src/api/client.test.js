import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import { ApiError, requestJson } from "./client.js";
import { checkBackendHealth } from "./health.js";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

test("requestJson returns parsed JSON from a successful response", async () => {
  globalThis.fetch = async () =>
    new Response(JSON.stringify({ status: "ok" }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });

  assert.deepEqual(await requestJson("/health"), { status: "ok" });
});

test("requestJson preserves structured backend error details", async () => {
  globalThis.fetch = async () =>
    new Response(
      JSON.stringify({ error: { code: "datasource_not_ready", message: "Scan required" } }),
      { status: 409, headers: { "Content-Type": "application/json" } },
    );

  await assert.rejects(requestJson("/api/ask"), (error) => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.status, 409);
    assert.equal(error.code, "datasource_not_ready");
    assert.equal(error.message, "Scan required");
    return true;
  });
});

test("requestJson reports invalid successful responses safely", async () => {
  globalThis.fetch = async () => new Response("not-json", { status: 200 });

  await assert.rejects(requestJson("/health"), (error) => {
    assert.equal(error.status, 200);
    assert.equal(error.code, "invalid_response");
    return true;
  });
});

test("requestJson maps network failures to backend_unavailable", async () => {
  globalThis.fetch = async () => {
    throw new TypeError("fetch failed");
  };

  await assert.rejects(requestJson("/health"), (error) => {
    assert.equal(error.status, 0);
    assert.equal(error.code, "backend_unavailable");
    return true;
  });
});

test("checkBackendHealth distinguishes available and unavailable backends", async () => {
  globalThis.fetch = async () =>
    new Response(JSON.stringify({ status: "ok", version: "0.1.0" }), { status: 200 });
  assert.deepEqual(await checkBackendHealth(), {
    availability: "available",
    version: "0.1.0",
    error: null,
  });

  globalThis.fetch = async () => {
    throw new TypeError("connection refused");
  };
  const unavailable = await checkBackendHealth();
  assert.equal(unavailable.availability, "unavailable");
  assert.equal(unavailable.version, null);
  assert.equal(unavailable.error.code, "backend_unavailable");
});
