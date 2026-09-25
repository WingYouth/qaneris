import { useEffect, useRef, useState } from "react";
import { messageTurns } from "../../conversation/conversationState.js";
import { runEventView } from "../../conversation/runEvents.js";
import { runFailure, runView, STATUS_LABELS } from "../../conversation/runView.js";
import { EvidencePanel } from "../ask/EvidencePanel.jsx";
import { VisualizationPanel } from "../visualization/VisualizationPanel.jsx";

function ClarificationCard({ view, onSubmit, disabled }) {
  const [answer, setAnswer] = useState("");
  const options = view?.clarification?.[0]?.options || [];
  const question = view?.clarification?.[0]?.question || view?.answer || "请补充信息以继续。";
  return <section className="clarification-card" aria-label="需要确认"><strong>{question}</strong>
    {options.length ? <div className="clarification-options">{options.map((option) => {
      const label = typeof option === "string" ? option : option?.label || option?.value || "";
      return <button key={label} type="button" disabled={disabled} onClick={() => onSubmit(label)}>{label}</button>;
    })}</div> : <form onSubmit={(event) => { event.preventDefault(); if (answer.trim()) onSubmit(answer.trim()); }}>
      <input aria-label="澄清回答" value={answer} onChange={(event) => setAnswer(event.target.value)} disabled={disabled} />
      <button type="submit" disabled={disabled || !answer.trim()}>确认</button>
    </form>}
  </section>;
}

function FederatedEvidence({ evidence }) {
  if (!evidence) return null;
  return <details className="evidence-panel"><summary>联合分析依据</summary>
    <p>多个数据源独立取数，不保证来自同一事务快照。</p>
    <dl className="identity-grid"><div><dt>合并方式</dt><dd>{evidence.merge_operation}</dd></div>
      {evidence.merge_mapping_id ? <div><dt>关联映射</dt><dd>{evidence.merge_mapping_id}</dd></div> : null}
      <div><dt>输出行数</dt><dd>{evidence.output_row_count}</dd></div></dl>
    {Object.keys(evidence.input_row_counts || {}).length ? <p>输入行数：{Object.entries(evidence.input_row_counts).map(([task, count]) => `${task} ${count} 行`).join(" · ")}</p> : null}
    {(evidence.source_evidence || []).map((source) => <div className="federated-source-evidence" key={source.task_id}>
      <strong>{source.datasource_id}</strong><span>{source.row_count} 行 · 扫描版本 {source.scan_version ?? "—"}</span>
      {source.source_started_at ? <small>取数开始：{source.source_started_at}</small> : null}
      {source.source_completed_at ? <small>取数完成：{source.source_completed_at}</small> : null}
    </div>)}
    {(evidence.warnings || []).map((warning, i) => <p key={i} className="chart-warning">{warning}</p>)}
  </details>;
}

function RunTimeline({ events }) {
  const items = events.map(runEventView).filter(Boolean);
  return <details className="run-timeline"><summary>执行过程 · {items.length} 个事件</summary>
    {items.length ? <ol>{items.map((item) => <li key={item.sequence}><span>{item.label}</span>{item.detail ? <small>{item.detail}</small> : null}</li>)}</ol> : <p>正在等待公开执行事件。</p>}
  </details>;
}

const SOURCE_STATUS = { PLANNED: "等待", RUNNING: "查询中", COMPLETED: "完成", FAILED: "失败", WAITING_USER: "待确认", SKIPPED: "跳过" };

function RunCard({ runId, run, events, message, onLoadRun, onAction, busy }) {
  const view = runView(run);
  const [opened, setOpened] = useState(false);
  const status = run?.status;
  return <article className="message message--assistant"><span className="message__role">SmartData</span>
    <div className="run-card__head"><span className="status-pill" aria-live="polite">{STATUS_LABELS[status] || (run ? status : "历史回答")}</span>
      {view?.kind === "diagnostic" ? <span>诊断分析</span> : view?.kind === "federated" ? <span>联合分析</span> : null}</div>
    {message?.content ? <p className="message__content">{message.content}</p> : view?.answer ? <p className="message__content">{view.answer}</p> : null}
    {status === "FAILED" || status === "BLOCKED" ? <p className="conversation-error" role="alert"><strong>{status === "BLOCKED" ? "无法继续：" : "执行失败："}</strong>{runFailure(run)}</p> : null}
    {status === "CANCELLED" ? <p>此轮已取消。</p> : null}
    {status === "WAITING_USER" ? <ClarificationCard view={view} disabled={busy} onSubmit={(answer) => onAction(runId, "clarify", answer)} /> : null}
    {run && status !== "COMPLETED" && status !== "FAILED" && status !== "BLOCKED" && status !== "CANCELLED" && status !== "WAITING_USER" ? <button type="button" className="secondary-button" disabled={busy} onClick={() => onAction(runId, "cancel")}>取消</button> : null}
    {status === "FAILED" && run.retryable ? <button type="button" className="secondary-button" disabled={busy} onClick={() => onAction(runId, "retry")}>重试</button> : null}
    {run?.run_kind === "federated" ? <div className="federated-progress"><strong>数据源任务</strong>
      {(view?.sourceTasks || []).map((task) => <div key={task.task_id}>{task.datasource_id}<span>{SOURCE_STATUS[task.status] || "处理中"}</span></div>)}
      <p>合并：{events.some((e) => e.event_type === "MERGE_COMPLETED") || status === "COMPLETED" ? "完成" : events.some((e) => e.event_type === "MERGE_STARTED") ? "执行中" : "等待"}</p>
    </div> : null}
    {run?.run_kind === "diagnostic" ? <div className="diagnostic-progress"><strong>证据问题进度</strong>
      {events.filter((e) => e.event_type === "EVIDENCE_QUESTION_PLANNED").map((event) => <p key={event.sequence}>{events.some((item) => item.event_type === "EVIDENCE_QUERY_COMPLETED" && item.public_payload?.task_id === event.public_payload?.task_id) ? "✓" : "→"} {event.public_payload?.question || "证据验证"}</p>)}
      {Array.isArray(view?.analysis?.evidence_refs) && view.analysis.evidence_refs.length ? <details><summary>已验证依据 · {view.analysis.evidence_refs.length}</summary><ul>{view.analysis.evidence_refs.map((ref) => <li key={ref}>{ref}</li>)}</ul></details> : null}
    </div> : null}
    {status === "COMPLETED" && view?.visualization ? <VisualizationPanel view={view.visualization} /> : null}
    {status === "COMPLETED" && view?.kind === "federated" ? <FederatedEvidence evidence={view.evidence} /> : null}
    {status === "COMPLETED" && view?.kind === "normal" ? <EvidencePanel plan={view.plan} evidence={view.evidence} /> : null}
    {!run && message ? <button type="button" className="secondary-button" onClick={() => { setOpened(true); onLoadRun(runId); }}>查看本轮结果与依据</button> : null}
    {run && opened && status !== "COMPLETED" ? <p>{STATUS_LABELS[status]}</p> : null}
    <RunTimeline events={events} />
  </article>;
}

export function MessageList({ messages, runs, events, latestRunId, onLoadRun, onAction, busy }) {
  const scroll = useRef(null);
  const follow = useRef(true);
  useEffect(() => { if (follow.current) scroll.current?.scrollTo({ top: scroll.current.scrollHeight, behavior: "smooth" }); }, [messages, events, runs]);
  const turns = messageTurns(messages);
  return <div className="message-list" ref={scroll} onScroll={(event) => { const el = event.currentTarget; follow.current = el.scrollHeight - el.scrollTop - el.clientHeight < 100; }} aria-label="消息历史">
    {!turns.length ? <div className="conversation-empty"><span>↗</span><h3>从一个问题开始</h3><p>答案、图表与执行依据会留在每一轮对话中。</p></div> : null}
    {turns.map((turn) => {
      const lastAssistant = [...turn.messages].reverse().find((message) => message.role === "assistant");
      return <div className="message-turn" key={turn.runId}>
        {turn.messages.map((message) => message.role === "user"
          ? <article className="message message--user" key={message.message_id}><span className="message__role">{message.message_kind === "clarification" ? "你的确认" : "你"}</span><p className="message__content">{message.content}</p></article>
          : message.message_id === lastAssistant?.message_id
            ? <RunCard key={message.message_id} runId={turn.runId} run={runs[turn.runId]} events={events[turn.runId] || []} message={message}
                onLoadRun={onLoadRun} onAction={onAction} busy={busy} />
            : <article className="message message--assistant" key={message.message_id}><span className="message__role">SmartData · 澄清</span><p className="message__content">{message.content}</p></article>)}
        {!lastAssistant ? <RunCard runId={turn.runId} run={runs[turn.runId]} events={events[turn.runId] || []}
          onLoadRun={onLoadRun} onAction={onAction} busy={busy} /> : null}
      </div>;
    })}
  </div>;
}
