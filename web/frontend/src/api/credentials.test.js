import test from "node:test";
import assert from "node:assert/strict";
import { createTextCredential } from "./credentials.js";

const originalFetch = globalThis.fetch;
test.afterEach(() => { globalThis.fetch = originalFetch; });

test("text credential values are sent only in the relative JSON request body", async () => {
  for (const kind of ["password", "token", "api_key"]) {
    const marker = `unique-${kind}-secret`;
    globalThis.fetch = async (url, init) => {
      assert.equal(url, "/api/credentials"); assert.equal(init.method, "POST");
      assert.deepEqual(JSON.parse(init.body), { kind, value: marker });
      assert.equal(url.includes(marker), false);
      return new Response('{"secret_id":"sec_test"}', { status: 201 });
    };
    assert.equal((await createTextCredential({ kind, value: marker })).secret_id, "sec_test");
  }
});
