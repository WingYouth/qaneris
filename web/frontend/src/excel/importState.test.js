import assert from "node:assert/strict";
import { test } from "node:test";

import {
  IMPORT_PHASE,
  importBytesSent,
  importErrorLocation,
  importErrorView,
  importFailed,
  importProgress,
  importResultView,
  importStarted,
  importSucceeded,
  initialImportState,
} from "./importState.js";

const READY_PAYLOAD = {
  status: "READY",
  import_id: "excel_abc_v1_deadbeef",
  workspace_id: "default",
  datasource_id: "ds_1",
  snapshot_id: "snap_1",
  scan_version: 1,
  original_filename: "orders.xlsx",
  file_sha256: "a".repeat(64),
  policy_version: "excel-ingestion-v1",
  neo4j_publication_verified: true,
  sheets: [
    {
      original_name: "orders",
      table_name: "orders",
      row_count: 5,
      column_count: 5,
      columns: [
        { source_header: "amount", column_name: "amount", inferred_type: "REAL", nullable: true, all_null: false },
      ],
    },
  ],
  warnings: ["跳过了隐藏工作表"],
};

test("an idle import reports no phase work", () => {
  const state = initialImportState();

  assert.equal(state.phase, IMPORT_PHASE.IDLE);
  assert.equal(state.progress, null);
  assert.equal(state.result, null);
});

test("the upload phase reports only real byte progress", () => {
  const started = importStarted(initialImportState(), { name: "orders.xlsx" });
  const progressed = importProgress(started, { loaded: 256, total: 1024 });
  const unknown = importProgress(started, { loaded: 256, total: 0 });

  assert.equal(started.phase, IMPORT_PHASE.UPLOADING);
  assert.equal(progressed.progress.percent, 25);
  assert.equal(unknown.progress.percent, null);
});

test("byte progress is ignored once the import has moved past uploading", () => {
  const ready = importSucceeded(importStarted(initialImportState(), {}), READY_PAYLOAD);

  assert.equal(importProgress(ready, { loaded: 1, total: 2 }), ready);
});

test("sending the bytes moves the import to the server-side phase", () => {
  const state = importBytesSent(importProgress(importStarted(initialImportState(), {}), { loaded: 1, total: 2 }));

  assert.equal(state.phase, IMPORT_PHASE.IMPORTING);
  assert.equal(state.progress.percent, 100);
  assert.equal(importBytesSent(initialImportState()).phase, IMPORT_PHASE.IDLE);
});

test("a READY import exposes product facts and no server location", () => {
  const view = importResultView(READY_PAYLOAD);

  assert.equal(view.status, "READY");
  assert.equal(view.datasourceId, "ds_1");
  assert.equal(view.snapshotId, "snap_1");
  assert.equal(view.scanVersion, 1);
  assert.equal(view.publicationVerified, true);
  assert.equal(view.filename, "orders.xlsx");
  assert.equal(view.sheets[0].tableName, "orders");
  assert.equal(view.sheets[0].columns[0].type, "REAL");
  assert.deepEqual(view.warnings, ["跳过了隐藏工作表"]);
  for (const internal of ["sqlite_path", "artifact_directory", "manifest_path"]) {
    assert.equal(internal in view, false);
  }
});

test("an Excel refusal keeps its stable code and its sheet/cell location", () => {
  const error = importErrorView({
    status: 400,
    payload: {
      error: {
        code: "EXCEL_FORMULA_UNSUPPORTED",
        message: "工作簿包含公式",
        sheet: "orders",
        coordinate: "D4",
      },
    },
  });

  assert.equal(error.code, "EXCEL_FORMULA_UNSUPPORTED");
  assert.equal(error.sheet, "orders");
  assert.equal(error.coordinate, "D4");
  assert.equal(importErrorLocation(error), "工作表 orders · 单元格 D4");
});

test("a transport failure still produces a usable error view", () => {
  const error = importErrorView({ code: "backend_unavailable", message: "无法连接", status: 0 });

  assert.equal(error.code, "backend_unavailable");
  assert.equal(error.sheet, null);
  assert.equal(importErrorLocation(error), "");
});

test("a failed attempt does not leave the previous workbook's result on screen", () => {
  const ready = importSucceeded(importStarted(initialImportState(), {}), READY_PAYLOAD);
  const failed = importFailed(ready, { code: "backend_unavailable", message: "无法连接", status: 0 });

  assert.equal(failed.phase, IMPORT_PHASE.ERROR);
  assert.equal(failed.result, null);
  assert.equal(failed.error.code, "backend_unavailable");
});
