const text = (value) => typeof value === "string" ? value : "";
export function traceItem(event) {
  const p = event?.payload || {};
  const summary = {
    accepted: "请求已接收",
    intent_ready: `意图已识别${Array.isArray(p.intent?.metrics) ? ` · 指标：${p.intent.metrics.join("、")}` : ""}${Array.isArray(p.intent?.dimensions) && p.intent.dimensions.length ? ` · 维度：${p.intent.dimensions.join("、")}` : ""}`,
    retrieval_ready: `检索完成${Number.isFinite(p.candidate_count) ? ` · 候选：${p.candidate_count}` : ""}`,
    grounding_ready: "语义依据已就绪",
    plan_ready: "查询计划已就绪",
    query_ready: text(p.display_command) || "查询语句已生成",
    execution_started: "开始执行查询",
    result_ready: p.response?.analysis?.result_kind === "schema_inventory"
      ? `结构读取完成${Number.isFinite(p.response?.result?.row_count) ? ` · ${p.response.result.row_count} 个对象` : ""}${p.response?.analysis?.summary_source === "model" ? " · 模型概述已生成" : ""}`
      : `查询完成${Number.isFinite(p.response?.result?.row_count) ? ` · ${p.response.result.row_count} 行` : ""}`,
    clarification_required: "需要补充说明",
    error: text(p.response?.error?.message) || "查询未能完成",
    done: "执行结束",
  };
  if (!(event?.event_type in summary)) return null;
  return { type: event.event_type, sequence: event.sequence, summary: summary[event.event_type] };
}
