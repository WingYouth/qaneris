import { askViewFromResponse, tableView } from "../ask/askView.js";

export const STATUS_LABELS = {
  CREATED: "已提交", CONTEXTUALIZING: "正在理解上下文", DISCOVERING: "正在发现数据",
  PLANNING: "正在生成受治理计划", EXECUTING: "正在查询真实数据", ANSWERING: "正在整理结果",
  WAITING_USER: "等待确认", COMPLETED: "已完成", FAILED: "执行失败", CANCELLED: "已取消", BLOCKED: "当前无法继续",
};
const BLOCKED_MESSAGES = {
  clarification_requires_external_action: "当前缺少已发布的业务定义。输入字段名称无法代替发布；请先为该字段确认并发布业务维度，再重新提问。",
  source_selection_ambiguous: "当前范围内有多个可用数据源。请在问题中写明数据源名称，或只选择一个数据源新建对话；跨库分析需明确各数据源及其业务口径。",
  source_selection_unresolved: "确认内容没有选出唯一数据源。请新建对话并只选择一个数据源，或在问题中写明数据源名称。",
  unconfirmed_mapping: "没有已确认的跨数据源关联键，请先确认关联映射。",
  stale_join_mapping: "跨数据源关联键的扫描版本已变化，请重新确认映射。",
  conversation_context_model_unavailable: "上下文理解服务暂不可用，请稍后再试。",
  federation_planner_unavailable: "联合分析计划暂不可用。",
  federation_semantics_missing: "所选数据源没有已发布的业务指标或维度。请先为参与的数据源发布业务定义，再进行联合分析。",
};
const FAILURE_MESSAGES = {
  graph_unavailable: "数据源结构与已发布图结构不一致，需要重新扫描数据源后再重试。",
};

export function graphRescanRequired(run) {
  if (run?.failure_code !== "graph_unavailable") return false;
  const message = run?.failure_message;
  return typeof message === "string"
    && (message.includes("重新扫描数据源") || message.includes("扫描目录与 Neo4j 已发布结构不一致"));
}

export function runFailure(run) {
  return BLOCKED_MESSAGES[run?.failure_code]
    || FAILURE_MESSAGES[run?.failure_code]
    || run?.failure_message
    || run?.failure_code
    || "执行未完成。";
}

export function runView(run) {
  if (!run) return null;
  const response = run.response_json;
  if (run.run_kind === "federated") {
    const merged = response?.merged_result;
    return { kind: "federated", answer: response?.answer || "", status: run.status,
      table: tableView(merged), visualization: merged ? { status: "completed", table: tableView(merged) } : null,
      plan: run.federated_plan || response?.federated_plan,
      sourceTasks: run.source_tasks || response?.source_tasks || [], evidence: response?.federated_evidence || null };
  }
  const ask = askViewFromResponse(response);
  return { kind: run.run_kind === "diagnostic" ? "diagnostic" : "normal", status: run.status,
    answer: ask?.answer || "", visualization: ask, clarification: ask?.clarification || [],
    plan: ask?.plan, evidence: ask?.evidence, analysis: response?.analysis || null };
}
