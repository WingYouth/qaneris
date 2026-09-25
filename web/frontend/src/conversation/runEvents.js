/** Only these public events have user-facing meanings. Never render arbitrary payloads. */
const LABELS = {
  RUN_STARTED: "开始执行", CONTEXT_READY: "上下文已准备", ASK_PROGRESS: "问数步骤进行中",
  DIAGNOSIS_STARTED: "开始诊断", DIAGNOSTIC_ROUND_STARTED: "开始一轮证据验证",
  EVIDENCE_QUESTION_PLANNED: "证据问题已安排", EVIDENCE_QUERY_STARTED: "正在验证证据问题",
  EVIDENCE_QUERY_COMPLETED: "证据问题已验证", EVIDENCE_QUERY_UNAVAILABLE: "证据暂不可用",
  EVIDENCE_QUERY_FAILED: "证据问题执行失败", DIAGNOSTIC_SYNTHESIS_STARTED: "正在整理诊断结论",
  DIAGNOSIS_COMPLETED: "诊断完成", FEDERATION_DISCOVERY_STARTED: "正在发现参与数据源",
  FEDERATED_PLAN_READY: "联合分析计划已确认", SOURCE_TASK_STARTED: "数据源查询中",
  SOURCE_TASK_COMPLETED: "数据源查询完成", SOURCE_TASK_FAILED: "数据源查询失败",
  MERGE_STARTED: "正在合并结果", MERGE_COMPLETED: "结果合并完成",
  CLARIFICATION_REQUIRED: "等待用户确认", RUN_COMPLETED: "回答完成", RUN_FAILED: "执行失败",
  RUN_BLOCKED: "当前无法继续", RUN_CANCELLED: "已取消",
};
const ASK_LABELS = {
  retrieval_ready: "语义检索完成", grounding_ready: "业务定义已确认", plan_ready: "查询计划已确认",
  query_ready: "只读查询已准备", execution_started: "正在执行查询", result_ready: "查询结果已返回",
};
export function runEventView(event) {
  const label = LABELS[event?.event_type];
  if (!label) return null;
  const p = event.public_payload || {};
  const detail = event.event_type === "ASK_PROGRESS" ? ASK_LABELS[p.ask_event_type] || null
    : event.event_type === "EVIDENCE_QUESTION_PLANNED" || event.event_type === "EVIDENCE_QUERY_STARTED" || event.event_type === "EVIDENCE_QUERY_COMPLETED"
      ? (typeof p.question === "string" ? p.question : null)
      : event.event_type.startsWith("SOURCE_TASK") ? (typeof p.datasource_id === "string" ? p.datasource_id : null) : null;
  return { sequence: event.sequence, label, detail };
}

export function shouldRefreshRun(event) {
  if (["RUN_STARTED", "CONTEXT_READY", "RUN_COMPLETED", "RUN_FAILED", "RUN_BLOCKED", "RUN_CANCELLED", "CLARIFICATION_REQUIRED",
    "SOURCE_TASK_STARTED", "SOURCE_TASK_COMPLETED", "SOURCE_TASK_FAILED", "FEDERATED_PLAN_READY", "MERGE_STARTED", "MERGE_COMPLETED",
    "DIAGNOSTIC_ROUND_STARTED", "DIAGNOSTIC_SYNTHESIS_STARTED"].includes(event.event_type)) return true;
  return event.event_type === "ASK_PROGRESS" && ["plan_ready", "execution_started", "result_ready"].includes(event.public_payload?.ask_event_type);
}
