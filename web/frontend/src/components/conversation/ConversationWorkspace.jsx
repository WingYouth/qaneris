import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { cancelRun, clarifyRun, createConversation, createRun, getConversation, getRun, listConversations, retryRun } from "../../api/conversations.js";
import { scanDatasource } from "../../api/datasources.js";
import { subscribeRunEvents } from "../../api/runStream.js";
import { conversationReducer, conversationTitle, initialConversationState, latestRunId, RUN_BUSY } from "../../conversation/conversationState.js";
import { shouldRefreshRun } from "../../conversation/runEvents.js";
import { ConversationSidebar } from "./ConversationSidebar.jsx";
import { MessageList } from "./MessageList.jsx";
import { MessageComposer } from "./MessageComposer.jsx";

const selectedFromUrl = () => new URLSearchParams(window.location.search).get("conversation");
const rememberSelection = (id) => {
  const url = new URL(window.location.href);
  url.searchParams.set("conversation", id);
  window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
};
const requestId = () => globalThis.crypto?.randomUUID?.() || `web-${Date.now()}-${Math.random().toString(36).slice(2)}`;

export function ConversationWorkspace({ workspaceId, datasources, backendAvailable, suggestedScope = "" }) {
  const [list, setList] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [state, dispatch] = useReducer(conversationReducer, undefined, initialConversationState);
  const [scope, setScope] = useState(suggestedScope ? [suggestedScope] : []);
  const [creating, setCreating] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [streamError, setStreamError] = useState(null);
  const [observedRun, setObservedRun] = useState(null);
  const [observeVersion, setObserveVersion] = useState(0);
  const [draft, setDraft] = useState("");
  const pending = useRef(null);
  const cursor = useRef({});
  const lock = useRef(false);
  const selectedRef = useRef(null);

  const refreshList = useCallback(async () => setList(await listConversations(workspaceId)), [workspaceId]);
  const refreshDetail = useCallback(async (id) => {
    const detail = await getConversation(id);
    if (selectedRef.current !== id) return;
    dispatch({ type: "refresh", detail });
    const latest = latestRunId(detail.messages || []);
    if (latest) {
      const run = await getRun(latest);
      if (selectedRef.current === id) {
        dispatch({ type: "run", run });
        // Replay the latest run even when settled, so its public timeline survives a reload.
        setObservedRun(latest);
      }
    } else setObservedRun(null);
  }, []);

  useEffect(() => {
    let live = true;
    setList([]); setSelectedId(null); selectedRef.current = null;
    dispatch({ type: "open", detail: { conversation: null, messages: [] } });
    listConversations(workspaceId).then((items) => {
      if (!live) return;
      setList(items);
      const stored = selectedFromUrl();
      const chosen = items.find((item) => item.conversation_id === stored) || items[0];
      if (chosen) { selectedRef.current = chosen.conversation_id; setSelectedId(chosen.conversation_id); }
    }).catch(setError);
    return () => { live = false; };
  }, [workspaceId]);

  useEffect(() => {
    if (!selectedId) return;
    selectedRef.current = selectedId;
    rememberSelection(selectedId);
    cursor.current = {};
    setStreamError(null);
    dispatch({ type: "open", detail: { conversation: null, messages: [] } });
    refreshDetail(selectedId).catch(setError);
  }, [selectedId, workspaceId, refreshDetail]);

  useEffect(() => {
    if (!observedRun || !selectedId) return;
    const controller = new AbortController();
    let live = true;
    setStreamError(null);
    subscribeRunEvents({ runId: observedRun, afterSequence: cursor.current[observedRun] || 0, signal: controller.signal,
      onEvent: (event) => {
        if (!live) return;
        cursor.current[event.run_id] = event.sequence;
        dispatch({ type: "event", event });
        if (shouldRefreshRun(event)) getRun(observedRun).then((run) => { if (live) dispatch({ type: "run", run }); }).catch(() => {});
      },
    }).then(async () => {
      if (!live) return;
      await refreshDetail(selectedId);
      await refreshList();
    }).catch((cause) => { if (live && cause.name !== "AbortError") setStreamError(cause); });
    return () => { live = false; controller.abort(); };
  }, [observedRun, observeVersion, selectedId, refreshDetail, refreshList]);

  const selectConversation = (id) => { selectedRef.current = id; setSelectedId(id); setObservedRun(null); setError(null); };
  const newConversation = async () => {
    if (creating || !backendAvailable) return;
    setCreating(true); setError(null);
    try {
      const created = await createConversation({ workspaceId, datasourceIds: scope });
      await refreshList(); selectConversation(created.conversation_id);
    } catch (cause) { setError(cause); }
    finally { setCreating(false); }
  };
  const send = async () => {
    if (!backendAvailable || lock.current) return;
    const question = draft.trim();
    if (!question || !selectedId) return;
    lock.current = true; setBusy(true); setError(null);
    if (!pending.current || pending.current.question !== question || pending.current.conversationId !== selectedId)
      pending.current = { question, conversationId: selectedId, clientRequestId: requestId() };
    try {
      const created = await createRun(selectedId, pending.current);
      pending.current = null; setDraft("");
      await refreshDetail(selectedId); await refreshList();
      setObservedRun(created.run_id); setObserveVersion((n) => n + 1);
    } catch (cause) { setError(cause); }
    finally { lock.current = false; setBusy(false); }
  };
  const act = async (runId, action, answer) => {
    setError(null); setBusy(true);
    try {
      if (action === "clarify") await clarifyRun(runId, answer);
      if (action === "cancel") await cancelRun(runId);
      if (action === "retry") await retryRun(runId);
      if (action === "rescan-retry") {
        const datasourceIds = state.conversation?.datasource_scope || [];
        if (datasourceIds.length !== 1) {
          throw new Error("当前对话没有绑定唯一数据源，请前往“数据源”页面重新扫描后再重试。");
        }
        await scanDatasource(datasourceIds[0]);
        await retryRun(runId);
      }
      const run = await getRun(runId);
      dispatch({ type: "run", run });
      await refreshDetail(selectedId);
      if (action !== "cancel") { setObservedRun(runId); setObserveVersion((n) => n + 1); }
    } catch (cause) { setError(cause); }
    finally { setBusy(false); }
  };
  const loadRun = async (id) => {
    try {
      const run = await getRun(id);
      if (selectedRef.current !== selectedId) return;
      dispatch({ type: "run", run });
      await subscribeRunEvents({ runId: id, afterSequence: cursor.current[id] || 0, maxReconnects: 0,
        onEvent: (event) => { if (selectedRef.current === selectedId) { cursor.current[id] = event.sequence; dispatch({ type: "event", event }); } } });
    } catch (cause) { setError(cause); }
  };
  const latest = latestRunId(state.messages);
  const active = state.runs[latest];
  const inputDisabled = !backendAvailable || busy || (active && RUN_BUSY.has(active.status));
  const ready = datasources.filter((item) => item.status === "ready");
  return <section className="conversation-workspace" aria-label="对话问数">
    <ConversationSidebar conversations={list} selectedId={selectedId} onSelect={selectConversation}
      onCreate={newConversation} creating={creating} disabled={!backendAvailable} ready={ready} scope={scope} onScopeChange={setScope} />
    <div className="conversation-main">
      <header className="conversation-header"><div><span className="step-label">CONVERSATION</span><h2>{conversationTitle(state.conversation, state.messages)}</h2>
        <small>{state.conversation ? (state.conversation.datasource_scope.length ? `${state.conversation.datasource_scope.length} 个指定数据源` : "工作区自动选择") : "选择或创建对话"}</small></div>
        <button className="secondary-button" type="button" onClick={() => selectedId && refreshDetail(selectedId).catch(setError)} disabled={!selectedId}>刷新</button>
      </header>
      {error ? <p className="conversation-error" role="alert">{error.message || "请求失败"}</p> : null}
      {streamError ? <p className="conversation-error" role="alert">事件连接中断：{streamError.message} <button type="button" onClick={() => setObserveVersion((n) => n + 1)}>重新连接</button></p> : null}
      <MessageList messages={state.messages} runs={state.runs} events={state.events} latestRunId={latest}
        onLoadRun={loadRun} onAction={act} busy={busy}
        canRescan={state.conversation?.datasource_scope?.length === 1} />
      <MessageComposer value={draft} onChange={(next) => { setDraft(next); if (pending.current?.question !== next.trim()) pending.current = null; }} onSend={send}
        disabled={inputDisabled || !selectedId} hint={!backendAvailable ? "后端不可用，暂不能发送新问题" : active && RUN_BUSY.has(active.status) ? "当前问题处理中" : !selectedId ? "请先创建对话" : "Enter 发送 · Shift+Enter 换行"} />
    </div>
  </section>;
}
