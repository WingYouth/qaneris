/**
 * The minimal execution evidence of one answered question.
 *
 * The plan is shown from the grounded plan itself (objects, aggregates, grouping) and the executed
 * query from ``evidence.display_command``, which the backend already states is safe to display. This
 * is not the full Trace workspace: it is the smallest honest view that shows the answer came from a
 * grounded plan and a read-only execution.
 */
export function EvidencePanel({ plan, evidence }) {
  if (!plan && !evidence) {
    return null;
  }

  return (
    <details className="evidence-panel">
      <summary>查询与执行证据</summary>

      {plan?.kind === "grounded" ? (
        <dl className="identity-grid">
          <div>
            <dt>计划 ID</dt>
            <dd>{plan.planId}</dd>
          </div>
          <div>
            <dt>扫描版本</dt>
            <dd>{plan.scanVersion ?? "—"}</dd>
          </div>
          <div>
            <dt>数据对象</dt>
            <dd>{plan.objects.join(", ") || "—"}</dd>
          </div>
          <div>
            <dt>聚合</dt>
            <dd>
              {plan.aggregates.map((item) => `${item.function}(${item.field})`).join(", ") || "—"}
            </dd>
          </div>
          <div>
            <dt>分组字段</dt>
            <dd>{plan.groupBy.join(", ") || "—"}</dd>
          </div>
        </dl>
      ) : null}

      {evidence?.displayCommand ? (
        <div className="evidence-panel__query">
          <span className="field__label">已校验的只读查询</span>
          <code>{evidence.displayCommand}</code>
        </div>
      ) : null}

      {evidence ? (
        <p className="evidence-panel__meta">
          {evidence.rowCount ?? "—"} 行 · 扫描版本 {evidence.scanVersion ?? "—"} ·{" "}
          {evidence.datasourceId}
        </p>
      ) : null}
    </details>
  );
}
