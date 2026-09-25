/**
 * The product view of one `AskResponse`.
 *
 * This is a projection of the existing typed contract, not a second interpretation of it: the four
 * statuses the backend can report are represented as they are, a clarification is shown as the text
 * the backend asked, and the result table renders exactly the columns and rows that came back.
 *
 * Values are rendered as text. Nothing here executes a column, a row, a plan or a model answer, and
 * no HTML from the backend is treated as markup.
 */

export const ASK_PHASE = {
  IDLE: "idle",
  ASKING: "asking",
  COMPLETED: "completed",
  CLARIFICATION: "clarification_required",
  FAILED: "failed",
  ERROR: "error",
};

export function initialAskState() {
  return { phase: ASK_PHASE.IDLE, response: null, error: null, question: "" };
}

export function askStarted(state, question) {
  return { ...initialAskState(), phase: ASK_PHASE.ASKING, question };
}

export function askFailed(state, error) {
  return { ...state, phase: ASK_PHASE.ERROR, response: null, error };
}

/** The backend's own status decides the phase: completed, clarification_required or failed. */
export function askCompleted(state, payload) {
  const view = askViewFromResponse(payload);
  return {
    ...state,
    phase: view?.status || ASK_PHASE.FAILED,
    response: view,
    error: null,
  };
}

export function askViewFromResponse(payload) {
  if (!payload || typeof payload !== "object") {
    return null;
  }
  return {
    status: payload.status,
    question: payload.question || "",
    answer: payload.answer || "",
    summarySource: payload.analysis?.result_kind === "schema_inventory"
      ? payload.analysis?.summary_source || "scan" : null,
    clarification: (payload.clarification || []).map((item) => ({
      question: item?.question || "",
      options: item?.options || [],
    })),
    table: tableView(payload.result),
    plan: planView(payload.plan),
    evidence: evidenceView(payload.evidence),
    error: payload.error ? { code: payload.error.code, message: payload.error.message } : null,
  };
}

export function tableView(result) {
  if (!result || typeof result !== "object") {
    return null;
  }
  const columns = Array.isArray(result.columns) ? result.columns : [];
  const rows = Array.isArray(result.rows) ? result.rows : [];
  return {
    columns,
    rows,
    rowCount: typeof result.row_count === "number" ? result.row_count : rows.length,
    truncated: result.truncated === true,
  };
}

/**
 * The grounded plan, summarised for display.
 *
 * The plan carries graph identities and no native query text at all, so what a user can see here is
 * what the plan really holds: which object, which aggregates, which grouping. The executed query
 * itself is shown from ``evidence.display_command``, which is where the backend puts it.
 */
export function planView(plan) {
  if (!plan) {
    return null;
  }
  if (Array.isArray(plan)) {
    return { kind: "legacy", steps: plan.map((step) => step?.query_language || "query") };
  }
  const objects = Object.values(plan.data_objects || {}).map((object) => object?.name || "");
  return {
    kind: "grounded",
    planId: plan.plan_id || "",
    scanVersion: plan.scan_version ?? null,
    objects: objects.filter(Boolean),
    aggregates: (plan.aggregates || []).map((item) => ({
      function: item?.function || "",
      field: item?.field?.field_path || "",
      alias: item?.alias || "",
    })),
    groupBy: (plan.group_by || []).map((item) => item?.field_path || ""),
    timeField: plan.time_field?.field_path || null,
    filters: (plan.filters || []).length,
    resultType: plan.expected_result_type || null,
  };
}

/** `evidence.display_command` is the backend's own safe display of the executed query. */
export function evidenceView(evidence) {
  if (!evidence || typeof evidence !== "object") {
    return null;
  }
  return {
    displayCommand: evidence.display_command || "",
    rowCount: typeof evidence.row_count === "number" ? evidence.row_count : null,
    truncated: evidence.truncated === true,
    datasourceId: evidence.datasource_id || "",
    scanVersion: evidence.scan_version ?? null,
  };
}

export function askIsCompleted(view) {
  return view?.status === "completed";
}

export function askNeedsClarification(view) {
  return view?.status === "clarification_required";
}
