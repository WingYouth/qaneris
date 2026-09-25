import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import { ApiError } from "./client.js";
import {
  EXCEL_IMPORT_ENDPOINT,
  IMPORT_FORM_FIELDS,
  buildImportFormData,
  importExcel,
  listDatasources,
} from "./excel.js";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

function fakeFile(name = "orders.xlsx", bytes = "workbook") {
  return new File([bytes], name, {
    type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  });
}

test("buildImportFormData sends only the workbook, the name and the workspace", () => {
  const form = buildImportFormData({ file: fakeFile(), name: "Roadshow", workspaceId: "default" });

  assert.deepEqual([...form.keys()], IMPORT_FORM_FIELDS);
  assert.equal(form.get("name"), "Roadshow");
  assert.equal(form.get("workspace_id"), "default");
  assert.equal(form.get("file").name, "orders.xlsx");
});

test("buildImportFormData never carries a server path or a validation switch", () => {
  const form = buildImportFormData({ file: fakeFile(), name: null, workspaceId: "default" });
  const fields = [...form.keys()];

  assert.deepEqual(fields, ["file", "workspace_id"]);
  for (const forbidden of [
    "file_path",
    "path",
    "force_new",
    "skip_validation",
    "no_scan",
    "no_neo4j",
    "workspace",
  ]) {
    assert.equal(form.has(forbidden), false, `${forbidden} must not be a multipart field`);
  }
});

test("importExcel posts the multipart body to the single Excel import endpoint", async () => {
  const calls = [];
  const payload = { status: "READY", datasource_id: "ds_1" };
  const transport = async (url, form, options) => {
    calls.push({ url, form, options });
    return payload;
  };

  const result = await importExcel({
    file: fakeFile(),
    name: "Roadshow",
    workspaceId: "default",
    transport,
  });

  assert.equal(result, payload);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, EXCEL_IMPORT_ENDPOINT);
  assert.deepEqual([...calls[0].form.keys()], IMPORT_FORM_FIELDS);
});

test("importExcel defaults the workspace and reports upload progress through the transport", async () => {
  const seen = [];
  const transport = async (url, form, options) => {
    options.onUploadProgress({ loaded: 512, total: 1024 });
    options.onUploadComplete();
    return { status: "READY" };
  };

  await importExcel({
    file: fakeFile(),
    transport,
    onUploadProgress: (progress) => seen.push(progress),
    onUploadComplete: () => seen.push("sent"),
  });

  assert.deepEqual(seen, [{ loaded: 512, total: 1024 }, "sent"]);
});

test("importExcel surfaces the stable Excel error code from the backend", async () => {
  const transport = async () => {
    throw new ApiError("公式不受支持", {
      status: 400,
      code: "excel_ingestion_failed",
      payload: {
        error: {
          code: "EXCEL_FORMULA_UNSUPPORTED",
          message: "公式不受支持",
          sheet: "orders",
          coordinate: "D4",
        },
      },
    });
  };

  await assert.rejects(importExcel({ file: fakeFile(), transport }), (error) => {
    assert.equal(error.status, 400);
    assert.equal(error.payload.error.code, "EXCEL_FORMULA_UNSUPPORTED");
    assert.equal(error.payload.error.sheet, "orders");
    assert.equal(error.payload.error.coordinate, "D4");
    return true;
  });
});

test("listDatasources reads the existing datasource list for one workspace", async () => {
  const seen = [];
  globalThis.fetch = async (url) => {
    seen.push(url);
    return new Response(JSON.stringify([{ id: "ds_1", status: "ready" }]), { status: 200 });
  };

  assert.deepEqual(await listDatasources("roadshow"), [{ id: "ds_1", status: "ready" }]);
  assert.deepEqual(seen, ["/api/datasources?workspace_id=roadshow"]);
});
