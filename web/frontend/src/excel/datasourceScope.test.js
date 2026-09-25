import assert from "node:assert/strict";
import { test } from "node:test";

import {
  readyDatasources,
  scopeAfterImport,
  scopeAfterRefresh,
  scopeDatasource,
  scopeLabel,
} from "./datasourceScope.js";

const READY = { id: "ds_1", name: "Roadshow Orders", status: "ready" };
const PENDING = { id: "ds_2", name: "Half scanned", status: "scanning" };

test("a finished import selects the datasource it created", () => {
  assert.equal(scopeAfterImport({ datasourceId: "ds_9" }), "ds_9");
  assert.equal(scopeAfterImport(null), "");
});

test("only READY datasources are offered as a scope", () => {
  assert.deepEqual(readyDatasources([READY, PENDING]), [READY]);
});

test("a refresh keeps the current scope while it is still usable", () => {
  assert.equal(scopeAfterRefresh([READY, PENDING], "ds_1"), "ds_1");
});

test("a refresh replaces a scope that disappeared with the first READY datasource", () => {
  assert.equal(scopeAfterRefresh([READY], "ds_gone"), "ds_1");
  assert.equal(scopeAfterRefresh([PENDING], "ds_1"), "");
  assert.equal(scopeAfterRefresh([], "ds_1"), "");
});

test("the scope label names the datasource and its id", () => {
  assert.equal(scopeLabel([READY], "ds_1"), "Roadshow Orders（ds_1）");
  assert.equal(scopeLabel([READY], "ds_missing"), "");
  assert.equal(scopeDatasource([READY, PENDING], "ds_2"), PENDING);
});
