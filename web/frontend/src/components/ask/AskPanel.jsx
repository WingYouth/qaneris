import { ASK_PHASE } from "../../ask/askView.js";
import { ClarificationPanel } from "./ClarificationPanel.jsx";
import { EvidencePanel } from "./EvidencePanel.jsx";
import { VisualizationPanel } from "../visualization/VisualizationPanel.jsx";

const PHASE_LABEL = {
  [ASK_PHASE.IDLE]: "待提问",
  [ASK_PHASE.ASKING]: "查询中",
  [ASK_PHASE.COMPLETED]: "已完成",
  [ASK_PHASE.CLARIFICATION]: "待确认",
  [ASK_PHASE.FAILED]: "未完成",
  [ASK_PHASE.ERROR]: "请求失败",
};

/**
 * The minimal Ask slice for the Excel Roadshow flow.
 *
 * It consumes the existing ``POST /api/ask`` contract as it is: completed, clarification_required,
 * failed, or a transport/api error. The question is always scoped to the selected datasource, and
 * the browser has no way to supply SQL, a plan or a field list.
 */
export function AskPanel({ state, scopeLabel, disabled, question, onQuestionChange, onSubmit }) {
  const view = state.response;

  return <>
    <section className="surface-card ask-card" aria-labelledby="ask-title">
      <div className="foundation-card__header"><span className="step-label">NEW QUERY</span><span className={`status-pill status-pill--ask-${state.phase}`}>{PHASE_LABEL[state.phase] || state.phase}</span></div>
      <h2 id="ask-title">提出一个问题</h2>
      <p className="ask-card__scope">当前数据源 <strong>{scopeLabel || "尚未选择就绪数据源"}</strong></p>
      <form className="ask-form" onSubmit={(event) => { event.preventDefault(); onSubmit(); }}>
        <label className="field"><span className="field__label">你的问题</span><textarea className="field__input" rows={3} value={question} placeholder="例如：这个数据库有哪些表？或按地区汇总销售额" onChange={(event) => onQuestionChange(event.target.value)} /></label>
        <div className="ask-form__footer"><span>基于已选数据源执行只读查询</span><button className="primary-button" type="submit" disabled={disabled || state.phase === ASK_PHASE.ASKING || !question.trim()}>{state.phase === ASK_PHASE.ASKING ? "查询中…" : "开始问数"}</button></div>
      </form>
      {state.phase === ASK_PHASE.ASKING ? <p className="ask-progress" role="status" aria-live="polite">正在检索数据依据并整理回答…</p> : null}
      {state.error ? <div className="import-error" role="alert"><strong>{state.error.code}</strong><p>{state.error.message}</p></div> : null}
    </section>
    {view ? <section className="surface-card result-card" aria-label="查询结果"><div className="result-card__header"><span className="step-label">RESULT</span><span className="result-card__status">{view.status === "completed" ? "已完成" : view.status === "clarification_required" ? "待确认" : "未完成"}</span></div><div className="ask-response">{view.summarySource ? <span className="ask-response__source">{view.summarySource === "model" ? "模型概述 · 基于扫描结构" : view.summarySource === "model_unavailable" ? "模型概述不可用 · 已显示扫描结构" : "扫描结构摘要"}</span> : null}<p className="ask-response__answer">{view.answer}</p>
      {view.error ? <div className="import-error" role="alert"><strong>{view.error.code}</strong><p>{view.error.message}</p></div> : null}
      <ClarificationPanel clarification={view.clarification} onUseOption={onQuestionChange} />
      <VisualizationPanel view={view} />
      <EvidencePanel plan={view.plan} evidence={view.evidence} />
    </div></section> : <section className="surface-card result-placeholder" aria-label="等待查询结果"><span className="step-label">RESULT</span><div><span className="result-placeholder__mark" aria-hidden="true">↗</span><h3>结果将在这里呈现</h3><p>输入问题后，答案、图表和执行依据会集中展示。</p></div></section>}
  </>;
}
