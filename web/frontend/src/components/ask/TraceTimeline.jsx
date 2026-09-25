export function TraceTimeline({ items = [] }) {
  return <section className="surface-card trace-card" aria-live="polite" aria-label="执行过程">
    <div className="trace-card__header"><div><span className="eyebrow">PROCESS</span><h2>执行过程</h2></div><span className="trace-card__count">{items.length || "—"}</span></div>
    {items.length ? <ol className="trace-list">{items.map((item) => <li key={`${item.sequence}-${item.type}`} className={item.type === "query_ready" ? "trace-list__query" : ""}><span>{item.summary}</span></li>)}</ol> : <div className="trace-card__empty"><span className="trace-card__empty-mark" aria-hidden="true">· · ·</span><p>提交问题后，查询步骤会显示在这里。</p></div>}
  </section>;
}
