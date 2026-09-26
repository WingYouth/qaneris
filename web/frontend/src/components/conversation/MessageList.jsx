import { useEffect, useMemo, useRef, useState } from "react";
import { messageTurns } from "../../conversation/conversationState.js";
import { runEventView } from "../../conversation/runEvents.js";
import { graphRescanRequired, runFailure, runView, STATUS_LABELS } from "../../conversation/runView.js";
import { EvidencePanel } from "../ask/EvidencePanel.jsx";
import { VisualizationPanel } from "../visualization/VisualizationPanel.jsx";

/* --------------------------------------------------------------------------------------------
   A small renderer for the answer text.

   The model writes light Markdown (bold, inline code, bullet lists, pipe tables). Rendering it
   here keeps the answer readable instead of showing raw pipes and asterisks. Anything the parser
   does not recognise stays literal text, and paragraph line breaks are preserved, so a plain-text
   answer is never mangled.
   -------------------------------------------------------------------------------------------- */

const INLINE = /(\*\*[^*]+\*\*|`[^`]+`)/g;

function inline(text, key) {
  return String(text).split(INLINE).filter((part) => part !== "").map((part, index) => {
    if (part.length > 4 && part.startsWith("**") && part.endsWith("**")) return <strong key={`${key}-${index}`}>{part.slice(2, -2)}</strong>;
    if (part.length > 2 && part.startsWith("`") && part.endsWith("`")) return <code key={`${key}-${index}`}>{part.slice(1, -1)}</code>;
    return part;
  });
}

const isRow = (line) => { const trimmed = line.trim(); return trimmed.startsWith("|") && trimmed.endsWith("|") && trimmed.length > 2; };
const isBullet = (line) => /^\s*[-*+]\s+\S/.test(line);
const isHeading = (line) => /^#{1,6}\s+\S/.test(line);

export function parseAnswerBlocks(text) {
  const lines = String(text ?? "").replace(/\r\n?/g, "\n").split("\n");
  const blocks = [];
  let index = 0;
  while (index < lines.length) {
    if (!lines[index].trim()) { index += 1; continue; }
    if (isRow(lines[index])) {
      const rows = [];
      while (index < lines.length && isRow(lines[index])) { rows.push(lines[index]); index += 1; }
      const cells = rows.map((row) => row.trim().slice(1, -1).split("|").map((cell) => cell.trim()));
      const head = cells[0] || [];
      const body = cells.slice(1).filter((row) => !row.every((cell) => /^:?-{2,}:?$/.test(cell)));
      if (head.length) blocks.push({ type: "table", head, body });
      continue;
    }
    if (isHeading(lines[index])) { blocks.push({ type: "heading", text: lines[index].replace(/^#{1,6}\s+/, "") }); index += 1; continue; }
    if (isBullet(lines[index])) {
      const items = [];
      while (index < lines.length && isBullet(lines[index])) { items.push(lines[index].replace(/^\s*[-*+]\s+/, "")); index += 1; }
      blocks.push({ type: "list", items });
      continue;
    }
    const paragraph = [];
    while (index < lines.length && lines[index].trim() && !isRow(lines[index]) && !isBullet(lines[index]) && !isHeading(lines[index])) {
      paragraph.push(lines[index].trim()); index += 1;
    }
    blocks.push({ type: "p", text: paragraph.join("\n") });
  }
  return blocks;
}

function AnswerBody({ text }) {
  const blocks = useMemo(() => parseAnswerBlocks(text), [text]);
  if (!blocks.length) return null;
  return <div className="answer-body">{blocks.map((block, position) => {
    const key = `b${position}`;
    if (block.type === "table") return <div className="answer-table" key={key}>
      <table><thead><tr>{block.head.map((cell, i) => <th key={i}>{inline(cell, `${key}-h${i}`)}</th>)}</tr></thead>
        <tbody>{block.body.map((row, r) => <tr key={r}>{block.head.map((_, c) => <td key={c}>{inline(row[c] ?? "", `${key}-${r}-${c}`)}</td>)}</tr>)}</tbody></table>
    </div>;
    if (block.type === "list") return <ul key={key}>{block.items.map((item, i) => <li key={i}>{inline(item, `${key}-${i}`)}</li>)}</ul>;
    if (block.type === "heading") return <h4 key={key}>{inline(block.text, key)}</h4>;
    return <p key={key}>{inline(block.text, key)}</p>;
  })}</div>;
}

const clock = (value) => {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
};

/** The short pill beside "Qaneris". Long stage names stay in the execution timeline. */
function pillFor(run) {
  const status = run?.status;
  if (!status) return { label: "历史回答", tone: "muted" };
  if (status === "COMPLETED") return { label: "执行完成", tone: "ok" };
  if (status === "FAILED") return { label: "执行失败", tone: "fail" };
  if (status === "BLOCKED") return { label: "无法继续", tone: "fail" };
  if (status === "CANCELLED") return { label: "已取消", tone: "muted" };
  if (status === "WAITING_USER") return { label: "待确认", tone: "wait" };
  return { label: "执行中", tone: "run" };
}

const PROGRESS_COPY = {
  CREATED: "已提交问题，正在准备…",
  CONTEXTUALIZING: "正在理解上下文，请稍候…",
  DISCOVERING: "正在发现可用数据，请稍候…",
  PLANNING: "正在生成受治理的查询计划，请稍候…",
  EXECUTING: "正在分析数据，请稍候…",
  ANSWERING: "正在整理结果，请稍候…",
};

function QanerisAvatar() {
  return <span className="chat-avatar" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
    <path d="m12 3 1.7 5.8L19.5 10l-5.8 1.7L12 17.5l-1.7-5.8L4.5 10l5.8-1.2L12 3Z" />
  </svg></span>;
}

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
  return <details className="run-timeline"><summary>详细说明 &amp; 执行过程 · {items.length} 个事件</summary>
    {items.length ? <ol>{items.map((item) => <li key={item.sequence}><span>{item.label}</span>{item.detail ? <small>{item.detail}</small> : null}</li>)}</ol> : <p>正在等待公开执行事件。</p>}
  </details>;
}

const SOURCE_STATUS = { PLANNED: "等待", RUNNING: "查询中", COMPLETED: "完成", FAILED: "失败", WAITING_USER: "待确认", SKIPPED: "跳过" };

/** A federated keyed_join is refused until its mapping is confirmed, so the error carries the way out. */
const MAPPING_FAILURES = new Set(["unconfirmed_mapping", "stale_join_mapping"]);

function RunCard({ runId, run, events, message, onLoadRun, onAction, busy, canRescan, onOpenGovernance }) {
  const view = runView(run);
  const [opened, setOpened] = useState(false);
  const status = run?.status;
  const pill = pillFor(run);
  const answer = message?.content || view?.answer || "";
  const rescanRequired = graphRescanRequired(run);
  return <article className="message message--assistant">
    <header className="message__head">
      <QanerisAvatar />
      <strong className="message__name">Qaneris</strong>
      <span className={`status-pill status-pill--${pill.tone}`} aria-live="polite">{pill.label}</span>
      {view?.kind === "diagnostic" ? <span className="message__tag">诊断分析</span> : view?.kind === "federated" ? <span className="message__tag">联合分析</span> : null}
      {message?.created_at ? <time className="message__time" dateTime={message.created_at}>{clock(message.created_at)}</time> : null}
    </header>
    {answer ? <AnswerBody text={answer} /> : null}
    {PROGRESS_COPY[status] ? <p className="run-progress">{PROGRESS_COPY[status]}</p> : null}
    {status === "FAILED" || status === "BLOCKED" ? <p className="conversation-error" role="alert"><strong>{status === "BLOCKED" ? "无法继续：" : "执行失败："}</strong>{runFailure(run)}
      {MAPPING_FAILURES.has(run?.failure_code) && onOpenGovernance ? <button className="inline-action" type="button" onClick={onOpenGovernance}>去确认关联映射</button> : null}
    </p> : null}
    {status === "CANCELLED" ? <p className="run-progress">此轮已取消。</p> : null}
    {status === "WAITING_USER" ? <ClarificationCard view={view} disabled={busy} onSubmit={(value) => onAction(runId, "clarify", value)} /> : null}
    {run && status !== "COMPLETED" && status !== "FAILED" && status !== "BLOCKED" && status !== "CANCELLED" && status !== "WAITING_USER" ? <button type="button" className="secondary-button" disabled={busy} onClick={() => onAction(runId, "cancel")}>取消</button> : null}
    {status === "FAILED" && run.retryable ? <button type="button" className="secondary-button" disabled={busy}
      title={rescanRequired && !canRescan ? "当前对话未绑定唯一数据源，请前往“数据源”页面重新扫描" : undefined}
      onClick={() => onAction(runId, rescanRequired ? "rescan-retry" : "retry")}>{rescanRequired ? "重新扫描并重试" : "重试"}</button> : null}
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

export function MessageList({ messages, runs, events, latestRunId, onLoadRun, onAction, busy, canRescan, onOpenGovernance }) {
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
          ? <article className="message message--user" key={message.message_id}>
            {message.message_kind === "clarification" ? <span className="message__role">你的确认</span> : null}
            <p className="message__content">{message.content}</p>
            {message.created_at ? <time className="message__time" dateTime={message.created_at}>{clock(message.created_at)}</time> : null}
          </article>
          : message.message_id === lastAssistant?.message_id
            ? <RunCard key={message.message_id} runId={turn.runId} run={runs[turn.runId]} events={events[turn.runId] || []} message={message}
                onLoadRun={onLoadRun} onAction={onAction} busy={busy} canRescan={canRescan} onOpenGovernance={onOpenGovernance} />
            : <article className="message message--assistant" key={message.message_id}>
              <header className="message__head"><QanerisAvatar /><strong className="message__name">Qaneris</strong><span className="status-pill status-pill--muted">澄清</span></header>
              <AnswerBody text={message.content} />
            </article>)}
        {!lastAssistant ? <RunCard runId={turn.runId} run={runs[turn.runId]} events={events[turn.runId] || []}
          onLoadRun={onLoadRun} onAction={onAction} busy={busy} canRescan={canRescan} onOpenGovernance={onOpenGovernance} /> : null}
      </div>;
    })}
  </div>;
}
