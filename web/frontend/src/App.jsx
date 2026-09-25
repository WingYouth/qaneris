import { useCallback, useEffect, useRef, useState } from "react";
import { streamAskQuestion } from "./api/askStream.js";
import { importExcel, listDatasources } from "./api/excel.js";
import { checkBackendHealth } from "./api/health.js";
import { askCompleted, askFailed, askStarted, initialAskState } from "./ask/askView.js";
import { traceItem } from "./ask/traceView.js";
import { AskPanel } from "./components/ask/AskPanel.jsx";
import { TraceTimeline } from "./components/ask/TraceTimeline.jsx";
import { BackendStatus } from "./components/common/BackendStatus.jsx";
import { DatasourceScope } from "./components/datasource/DatasourceScope.jsx";
import { DatasourceManager } from "./components/datasource/DatasourceManager.jsx";
import { ExcelImportCard } from "./components/excel/ExcelImportCard.jsx";
import { ProductNav } from "./components/navigation/ProductNav.jsx";
import { scopeAfterImport, scopeAfterRefresh, scopeLabel } from "./excel/datasourceScope.js";
import { importBytesSent, importFailed, importProgress, importResultView, importStarted, importSucceeded, initialImportState } from "./excel/importState.js";

const INITIAL_HEALTH = { availability: "checking", version: null, error: null };
const DEFAULT_WORKSPACE = "default";

function App() {
  const [activeView, setActiveView] = useState("ask"); const [health, setHealth] = useState(INITIAL_HEALTH); const [workspaceId, setWorkspaceId] = useState(DEFAULT_WORKSPACE);
  const [datasources, setDatasources] = useState([]); const [scope, setScope] = useState(""); const [file, setFile] = useState(null); const [datasourceName, setDatasourceName] = useState(""); const [importState, setImportState] = useState(initialImportState());
  const [question, setQuestion] = useState(""); const [askState, setAskState] = useState(initialAskState()); const [trace, setTrace] = useState([]);
  const askAbort = useRef(null);
  const selectScope = (next) => {
    if (next !== scope) { askAbort.current?.abort(); setAskState(initialAskState()); setTrace([]); }
    setScope(next);
  };
  const changeWorkspace = (next) => {
    if (next !== workspaceId) { askAbort.current?.abort(); setAskState(initialAskState()); setTrace([]); setScope(""); }
    setWorkspaceId(next);
  };
  useEffect(() => { askAbort.current?.abort(); setAskState(initialAskState()); setTrace([]); }, [scope, workspaceId]);
  const refreshHealth = useCallback(async () => setHealth(await checkBackendHealth()), []);
  const refreshDatasources = useCallback(async (workspace = workspaceId, current = scope) => {
    try { const listed = await listDatasources(workspace); setDatasources(listed); const next = scopeAfterRefresh(listed, current); setScope(next); return next; }
    catch { setDatasources([]); setScope(""); return ""; }
  }, [workspaceId, scope]);
  useEffect(() => { refreshHealth(); }, [refreshHealth]);
  useEffect(() => {
    listDatasources(workspaceId).then((listed) => {
      setDatasources(listed);
      setScope((current) => scopeAfterRefresh(listed, current));
    }).catch(() => { setDatasources([]); setScope(""); });
  }, [workspaceId, activeView]);
  const handleImport = useCallback(async () => {
    if (!file) return; askAbort.current?.abort(); setTrace([]); setImportState(importStarted(file)); setAskState(initialAskState());
    try { const payload = await importExcel({ file, name: datasourceName.trim() || undefined, workspaceId, onUploadProgress: (p) => setImportState((s) => importProgress(s, p)), onUploadComplete: () => setImportState((s) => importBytesSent(s)) }); const imported = importResultView(payload); setImportState((s) => importSucceeded(s, imported)); await refreshDatasources(workspaceId, scopeAfterImport(imported)); }
    catch (error) { setImportState((s) => importFailed(s, error)); }
  }, [file, datasourceName, workspaceId, refreshDatasources]);
  const handleAsk = useCallback(async () => {
    const trimmed = question.trim(); if (!trimmed || !scope) return;
    askAbort.current?.abort();
    const controller = new AbortController(); askAbort.current = controller;
    setTrace([]); setAskState((s) => askStarted(s, trimmed));
    try {
      let finalResponse = null;
      await streamAskQuestion({ question: trimmed, workspaceId, datasourceId: scope, signal: controller.signal, onEvent: (event) => { if (controller.signal.aborted) return; const item = traceItem(event); if (item) setTrace((old) => [...old, item]); if (["result_ready", "clarification_required", "error"].includes(event.event_type)) finalResponse = event.payload?.response || null; } });
      if (controller.signal.aborted) return;
      if (!finalResponse) throw Object.assign(new Error("Stream ended without a final Ask response."), { code: "invalid_event_stream" });
      setAskState((s) => askCompleted(s, finalResponse));
    } catch (error) { if (!controller.signal.aborted) setAskState((s) => askFailed(s, error)); }
  }, [question, workspaceId, scope]);
  const goAsk = (id) => { selectScope(id); setActiveView("ask"); };
  const offline = health.availability === "unavailable";
  const viewIntro = {
    ask: { number: "01 / 03", label: "ANALYZE", title: "让数据，回答问题。", description: "选定可信数据源，以自然语言发问。结果、图表与执行依据会在同一个工作区呈现。" },
    datasources: { number: "02 / 03", label: "CONNECT", title: "连接你的数据。", description: "管理数据源、检查连接并扫描结构，为每一次查询建立可靠的数据范围。" },
    excel: { number: "03 / 03", label: "IMPORT", title: "从表格开始。", description: "上传 Excel 工作簿，完成导入与扫描后，立即用自然语言探索其中的数据。" },
  };
  const intro = viewIntro[activeView];
  return <div className="app-shell">
    <aside className="sidebar">
      <a className="brand" href="#workspace"><span className="brand__mark" aria-hidden="true">S<span>·</span></span><span className="brand__copy"><strong>SmartData</strong><small>DATA INTELLIGENCE</small></span></a>
      <ProductNav activeView={activeView} onNavigate={setActiveView} />
      <div className="sidebar__bottom"><div className="workspace-card"><span className="workspace-card__label">CURRENT WORKSPACE</span><strong><span className="workspace-card__dot" />{workspaceId}</strong><span>安全的数据探索空间</span></div><span className="sidebar__signature">SMARTDATA / ROADSHOW</span></div>
    </aside>
    <main className="app-main" id="workspace">
      <header className="topbar"><div className="topbar__path"><span>工作台</span><span aria-hidden="true">/</span><strong>{intro.label}</strong></div><BackendStatus availability={health.availability} version={health.version} /></header>
      <div className="content">
        <section className="page-hero" aria-labelledby="page-title"><span className="page-hero__eyebrow">{intro.number} &nbsp; {intro.label}</span><h1 id="page-title">{intro.title}</h1><p>{intro.description}</p></section>
        {offline ? <div role="alert" className="feedback feedback--error"><strong>SmartData 后端不可用</strong><p>{health.error?.message || "请检查 FastAPI 服务。"}</p><button onClick={refreshHealth}>重试</button></div> : null}
        {activeView === "ask" ? <div className="ask-workspace"><div className="ask-workspace__main"><DatasourceScope datasources={datasources} scope={scope} onScopeChange={selectScope} onRefresh={() => refreshDatasources(workspaceId, scope)} />
          <AskPanel state={askState} scopeLabel={scopeLabel(datasources, scope)} disabled={health.availability !== "available" || !scope} question={question} onQuestionChange={setQuestion} onSubmit={handleAsk} />
        </div><TraceTimeline items={trace} /></div> : null}
        {activeView === "datasources" ? <DatasourceManager workspaceId={workspaceId} onAsk={goAsk} onWorkspaceChange={changeWorkspace} /> : null}
        {activeView === "excel" ? <><ExcelImportCard state={importState} fileName={file?.name || ""} datasourceName={datasourceName} workspaceId={workspaceId} disabled={health.availability !== "available"} onFileNameChange={setFile} onDatasourceNameChange={setDatasourceName} onWorkspaceChange={changeWorkspace} onSubmit={handleImport} />
          {importState.result?.status === "READY" ? <button className="primary-button" onClick={() => goAsk(importState.result.datasourceId)}>立即问数 <span aria-hidden="true">↗</span></button> : null}</> : null}
      </div>
    </main>
  </div>;
}
export default App;
