/**
 * Browser-contract acceptance for the Excel → Ask flow (EXCEL-01C).
 *
 * This script imports the *real* frontend modules and drives them against a *real* API server:
 *
 *   src/api/excel.js  -> importExcel()      (multipart upload, the same FormData the browser builds)
 *   src/api/excel.js  -> listDatasources()  (the datasource list the UI refreshes)
 *   src/api/ask.js    -> askQuestion()      (POST /api/ask, scoped to the returned datasource id)
 *   src/excel/*.js    -> importResultView / scopeAfterImport / importErrorView
 *   src/ask/askView.js-> askViewFromResponse (the result table and evidence the UI renders)
 *
 * What it therefore proves is the request contract the browser really sends and the response
 * projection the UI really renders - not a re-implementation of either.
 *
 * One deliberate difference from a browser session: the transport is built on fetch rather than on
 * XMLHttpRequest, because Node has no XHR. The URL, the multipart body and the response handling are
 * the same code path; only byte-progress reporting is unavailable, and no progress number is faked to
 * hide that. The XHR transport is what the browser uses at runtime.
 *
 * Usage:
 *   node web/frontend/scripts/acceptance_browser_flow.mjs \
 *     --base-url URL --workbook PATH [--formula-workbook PATH] [--question TEXT]
 */

import { readFile } from "node:fs/promises";
import { basename } from "node:path";

import { streamAskQuestion } from "../src/api/askStream.js";
import { importExcel, listDatasources } from "../src/api/excel.js";
import { createFetchTransport } from "../src/api/upload.js";
import { askViewFromResponse } from "../src/ask/askView.js";
import { traceItem } from "../src/ask/traceView.js";
import { scopeAfterImport, scopeLabel } from "../src/excel/datasourceScope.js";
import { automaticChartKind, eligibleChartKinds } from "../src/visualization/chartPolicy.js";
import { buildChartSpec } from "../src/visualization/chartSpec.js";
import {
  importBytesSent,
  importErrorView,
  importFailed,
  importProgress,
  importResultView,
  importStarted,
  importSucceeded,
  initialImportState,
} from "../src/excel/importState.js";

const DEFAULT_QUESTION = "对 orders 表，按 region 分组汇总 amount 的总和。";

function argument(name, fallback = null) {
  const index = process.argv.indexOf(`--${name}`);
  return index === -1 ? fallback : process.argv[index + 1];
}

function report(payload) {
  // One machine-readable document on the last line. Snake case, like every other JSON contract in
  // this project, so the acceptance can assert it without a translation layer.
  console.log(JSON.stringify(payload));
}

async function upload(transport, bytes, filename, workspaceId, name, trackers = {}) {
  const file = new File([bytes], filename, {
    type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  });
  let state = importStarted(initialImportState(), file);
  const payload = await importExcel({
    file,
    name,
    workspaceId,
    transport,
    onUploadProgress: (progress) => {
      state = importProgress(state, progress);
      trackers.onProgress?.(state.progress?.percent ?? null);
    },
    onUploadComplete: () => {
      state = importBytesSent(state);
    },
  });
  return importResultView(payload);
}

async function main() {
  const baseUrl = (argument("base-url") || "http://127.0.0.1:8000").replace(/\/$/, "");
  const workbookPath = argument("workbook");
  const formulaPath = argument("formula-workbook");
  const workspaceId = argument("workspace", "default");
  const question = argument("question", DEFAULT_QUESTION);
  if (!workbookPath) {
    throw new Error("--workbook is required");
  }

  // The API client builds relative URLs, exactly as it does behind the Vite proxy. A browser
  // resolves them against the page origin; Node has no origin, so the runner supplies one here.
  // This shim is the only concession to the runtime - the frontend modules themselves are untouched
  // and keep sending the same relative requests.
  const nativeFetch = globalThis.fetch;
  globalThis.fetch = (input, init) =>
    nativeFetch(typeof input === "string" ? new URL(input, baseUrl) : input, init);
  const transport = createFetchTransport(globalThis.fetch);

  const out = {
    phase: "started",
    status: null,
    datasource_id: null,
    snapshot_id: null,
    scope: null,
    scope_label: null,
    listed: [],
    asked_datasource_id: null,
    ask_status: null,
    ask_attempts: 0,
    clarification: [],
    table_rows: null,
    table_columns: [],
    row_count: null,
    display_command: null,
    plan_id: null,
    used_returned_datasource_id: false,
    progress_percent: null,
    error_code: null,
    error_location: null,
    error_state: null,
    trace_events: [],
    trace_summaries: [],
    clarification_stream_status: null,
    clarification_stream_events: [],
    asked_scan_version: null,
    trace_display_command: null,
    chart_auto_kind: null,
    chart_eligible_kinds: [],
    chart_spec: null,
  };

  // ---- upload the workbook exactly as the browser does --------------------------------------
  let imported;
  try {
    imported = await upload(
      transport,
      await readFile(workbookPath),
      basename(workbookPath),
      workspaceId,
      `Roadshow ${basename(workbookPath).replace(/\.xlsx$/i, "")}`,
      { onProgress: (percent) => { out.progress_percent = percent; } },
    );
  } catch (error) {
    out.phase = "error";
    out.error_code = importErrorView(error).code;
    report(out);
    return 1;
  }

  out.phase = "ready";
  out.status = imported.status;
  out.datasource_id = imported.datasourceId;
  out.snapshot_id = imported.snapshotId;
  out.scan_version = imported.scanVersion;
  out.publication_verified = imported.publicationVerified;

  // ---- refresh the list and let the imported datasource become the scope --------------------
  const listed = await listDatasources(workspaceId);
  out.listed = listed.map((item) => ({ id: item.id, status: item.status }));
  out.scope = scopeAfterImport(imported);
  out.scope_label = scopeLabel(listed, out.scope);
  out.used_returned_datasource_id = Boolean(out.scope) && out.scope === out.datasource_id;

  // ---- ask, scoped to the id the import returned --------------------------------------------
  //
  // Retried only when the call itself fails (a non-2xx response: a model or transport problem).
  // A clarification, a completed answer or a ``failed`` status all arrive as 200 and are never
  // retried - those are product outcomes and retrying them would hide one.
  let response;
  for (out.ask_attempts = 1; out.ask_attempts <= 3; out.ask_attempts += 1) {
    try {
      const events = [];
      await streamAskQuestion({ question, workspaceId, datasourceId: out.scope, onEvent: (event) => events.push(event) });
      out.trace_events = events.map((event) => event.event_type);
      out.trace_summaries = events.map(traceItem).filter(Boolean).map((item) => item.summary);
      const finalEvent = events.findLast((event) => ["result_ready", "clarification_required", "error"].includes(event.event_type));
      response = finalEvent?.payload?.response || null;
      if (!response) throw new Error("stream_final_response_missing");
      break;
    } catch (error) {
      if (out.ask_attempts === 3) {
        throw error;
      }
    }
  }
  const view = askViewFromResponse(response);
  out.ask_status = view.status;
  out.clarification = view.clarification.map((item) => item.question);
  out.asked_datasource_id = response.evidence?.datasource_id || response.plan?.datasource_id || null;
  out.table_columns = view.table?.columns || [];
  out.row_count = view.table?.rowCount ?? null;
  out.table_rows = Object.fromEntries(
    (view.table?.rows || []).map((row) => [row.region, row.sum_amount]),
  );
  out.display_command = view.evidence?.displayCommand || null;
  out.plan_id = view.plan?.planId || null;
  out.asked_scan_version = view.evidence?.scanVersion ?? null;
  out.trace_display_command = out.trace_summaries.find((line) => /^\s*(SELECT|WITH)\b/i.test(line)) || null;
  out.chart_auto_kind = automaticChartKind(view);
  out.chart_eligible_kinds = eligibleChartKinds(view);
  out.chart_spec = buildChartSpec(view, out.chart_auto_kind);

  // An ungoverned metric is a normal clarification outcome and still ends with a real done event.
  const clarificationEvents = [];
  await streamAskQuestion({ question: "对 orders 表，按 region 分组汇总 profit 的总和。", workspaceId, datasourceId: out.scope, onEvent: (event) => clarificationEvents.push(event) });
  out.clarification_stream_events = clarificationEvents.map((event) => event.event_type);
  const clarificationEvent = clarificationEvents.find((event) => event.event_type === "clarification_required");
  out.clarification_stream_status = clarificationEvent?.payload?.response?.status || null;

  // ---- the refusal view the UI renders for a rejected workbook -------------------------------
  if (formulaPath) {
    try {
      await upload(
        transport,
        await readFile(formulaPath),
        basename(formulaPath),
        workspaceId,
        "Rejected workbook",
      );
      out.error_code = null;
    } catch (error) {
      const refusal = importErrorView(error);
      out.error_code = refusal.code;
      out.error_location = `${refusal.sheet}/${refusal.coordinate}`;
    }
    out.error_state = importFailed(initialImportState(), {
      payload: { error: { code: out.error_code, message: "refused" } },
    }).error.code;
  }

  report(out);
  return 0;
}

main().then(
  (code) => process.exit(code),
  (error) => {
    console.error(error?.stack || String(error));
    report({ phase: "error", error_code: String(error?.message || error) });
    process.exit(1);
  },
);
