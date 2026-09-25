import test from "node:test";
import assert from "node:assert/strict";
import { readdir, readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const sourceRoot = dirname(fileURLToPath(import.meta.url));
async function files(path) {
  const entries = await readdir(path, { withFileTypes: true });
  return (await Promise.all(entries.map((entry) => entry.isDirectory() ? files(join(path, entry.name)) : entry.name.endsWith(".js") || entry.name.endsWith(".jsx") ? [join(path, entry.name)] : []))).flat();
}

test("frontend source does not persist secrets or execute dynamic HTML/code", async () => {
  const source = (await Promise.all((await files(sourceRoot)).filter((file) => !file.endsWith("/security.test.js")).map((file) => readFile(file, "utf8")))).join("\n");
  for (const forbidden of [/\blocalStorage\b/, /\bsessionStorage\b/, /dangerouslySetInnerHTML/, /\beval\s*\(/, /new\s+Function\s*\(/]) assert.doesNotMatch(source, forbidden);
  assert.doesNotMatch(source, /TLS_DRIVER_MATRIX/);
});
