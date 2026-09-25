import test from "node:test";
import assert from "node:assert/strict";
import { createSecureDatasource, deleteDatasource, updateSecureDatasource } from "./datasources.js";

const originalFetch = globalThis.fetch;
test.afterEach(() => { globalThis.fetch = originalFetch; });

test("secure datasource create preserves the profile contract and uses the secure endpoint", async () => {
  const body = { name: "sales", kind: "relational", workspace_id: "default", connection_profile: { driver: "sqlite", endpoint: { path: "/data/sales.db" } } };
  globalThis.fetch = async (url, init) => { assert.equal(url, "/api/datasources/secure"); assert.equal(init.method, "POST"); assert.deepEqual(JSON.parse(init.body), body); return new Response('{"id":"ds_1","status":"created"}', { status: 201 }); };
  assert.equal((await createSecureDatasource(body)).status, "created");
});

test("update uses PUT and delete accepts an empty 204 response", async () => {
  const calls = [];
  globalThis.fetch = async (url, init) => { calls.push([url, init.method]); return init.method === "PUT" ? new Response('{"id":"ds_1"}') : new Response(null, { status: 204 }); };
  await updateSecureDatasource("ds_1", { connection_profile: { driver: "sqlite" } });
  assert.equal(await deleteDatasource("ds_1"), null);
  assert.deepEqual(calls, [["/api/datasources/ds_1/secure", "PUT"], ["/api/datasources/ds_1", "DELETE"]]);
});
