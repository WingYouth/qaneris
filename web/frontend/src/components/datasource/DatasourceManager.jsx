import { useCallback, useEffect, useState } from "react";
import { deleteDatasource, inspectDatasource, listDatasources, scanDatasource, testSavedDatasource } from "../../api/datasources.js";
import { DatasourceWizard } from "./DatasourceWizard.jsx";

const STATUS_LABEL = { ready: "已扫描", created: "待扫描", failed: "需处理", scanning: "扫描中" };

export function DatasourceManager({ workspaceId = "default", onAsk, onWorkspaceChange, query = "" }) {
  const [items, setItems] = useState([]);
  const [selected, setSelected] = useState(null);
  const [wizard, setWizard] = useState(null);
  const [confirm, setConfirm] = useState(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState("");

  const refresh = useCallback(async () => {
    try { setItems(await listDatasources(workspaceId)); setError(null); }
    catch (failure) { setError(failure); }
  }, [workspaceId]);

  useEffect(() => { refresh(); }, [refresh]);

  const inspect = async (id) => {
    try { setSelected(await inspectDatasource(id)); setError(null); }
    catch (failure) { setError(failure); }
  };

  const scan = async (item) => {
    setBusy(item.id); setNotice(""); setError(null);
    try { await scanDatasource(item.id); await refresh(); await inspect(item.id); setNotice(`“${item.name}”扫描完成。`); }
    catch (failure) { setError(failure); }
    finally { setBusy(""); }
  };

  const test = async (item) => {
    setBusy(item.id); setNotice(""); setError(null);
    try { await testSavedDatasource(item.id); setNotice(`“${item.name}”连接正常。`); }
    catch (failure) { setError(failure); }
    finally { setBusy(""); }
  };

  const remove = async (item) => {
    setBusy(item.id); setNotice(""); setError(null);
    try {
      await deleteDatasource(item.id);
      setItems((current) => current.filter((source) => source.id !== item.id));
      setSelected((current) => current?.id === item.id ? null : current);
      setConfirm(null);
      setNotice(`“${item.name}”已删除。`);
      await refresh();
    } catch (failure) { setError(failure); }
    finally { setBusy(""); }
  };

  if (wizard) return <DatasourceWizard workspaceId={workspaceId} onWorkspaceChange={onWorkspaceChange}
    existing={wizard === "new" ? null : wizard} onCancel={() => { setWizard(null); refresh(); }}
    onSaved={async (id, targetWorkspace) => { setWizard(null); setItems(await listDatasources(targetWorkspace)); await inspect(id); setNotice("数据源已保存并扫描。"); }} />;

  const needle = query.trim().toLocaleLowerCase();
  const shown = needle
    ? items.filter((item) => `${item.name} ${item.driver || ""} ${item.kind || ""}`.toLocaleLowerCase().includes(needle))
    : items;

  return <div className="source-layout">
    <section className="surface-card source-panel" aria-labelledby="source-heading">
      <div className="section-heading"><div><span className="eyebrow">SOURCES</span><h2 id="source-heading">数据源</h2><p>连接、扫描并管理当前工作区的数据。</p></div><button className="primary-button" onClick={() => { setNotice(""); setWizard("new"); }}>添加数据源</button></div>
      {error ? <div className="feedback feedback--error" role="alert"><strong>操作未完成</strong><p>{error.message}</p></div> : null}
      {notice ? <p className="source-notice" role="status">{notice}</p> : null}
      {shown.length ? <div className="source-list">{shown.map((item) => <article className="source-item" key={item.id}>
        <div className="source-item__main"><button className="source-item__name" onClick={() => inspect(item.id)}>{item.name}</button><div className="source-item__meta"><span>{item.driver || "未知驱动"}</span><span>{item.kind}</span><span>{item.workspace_id}</span></div></div>
        <span className={`source-item__status source-item__status--${item.status}`}>{STATUS_LABEL[item.status] || item.status}</span>
        <div className="source-item__actions"><button onClick={() => inspect(item.id)}>详情</button><button disabled={Boolean(busy)} onClick={() => test(item)}>{busy === item.id ? "检查中…" : "测试连接"}</button>{item.status === "ready" ? <button onClick={() => onAsk(item.id)}>问数</button> : <button disabled={Boolean(busy)} onClick={() => scan(item)}>{busy === item.id ? "扫描中…" : "扫描"}</button>}<button onClick={() => setWizard(item)}>重新连接</button><button className="source-item__delete" disabled={Boolean(busy)} onClick={() => { setError(null); setConfirm(item); }}>删除</button></div>
      </article>)}</div> : <div className="source-empty"><strong>{items.length ? "没有匹配的数据源" : "还没有数据源"}</strong><p>{items.length ? "请调整顶部搜索关键词。" : "添加数据库连接，或从 Excel 导入工作簿。"}</p></div>}
    </section>
    {selected ? <aside className="surface-card source-detail"><div className="source-detail__heading"><span className="eyebrow">DETAILS</span><button className="source-detail__close" onClick={() => setSelected(null)} aria-label="关闭详情">×</button></div><h2>{selected.name}</h2><p>连接元数据保存在后端目录中。凭据不会返回浏览器。</p><dl>{[["数据源 ID", selected.id], ["驱动", selected.driver], ["类型", selected.kind], ["状态", STATUS_LABEL[selected.status] || selected.status], ["工作区", selected.workspace_id], ["扫描版本", selected.scan_version ?? "—"], ["数据集", selected.dataset_count ?? "—"], ["最近扫描", selected.last_scan_status ?? "—"]].map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value || "—"}</dd></div>)}</dl></aside> : null}
    {confirm ? <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) setConfirm(null); }}><section className="confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="delete-title"><span className="eyebrow">DELETE SOURCE</span><h2 id="delete-title">删除“{confirm.name}”？</h2><p>这会删除数据源记录及其扫描结果，并清理对应的图数据。操作完成后无法从页面恢复。</p><div className="confirm-dialog__actions"><button disabled={Boolean(busy)} onClick={() => setConfirm(null)}>取消</button><button className="danger-button" disabled={Boolean(busy)} onClick={() => remove(confirm)}>{busy === confirm.id ? "删除中…" : "确认删除"}</button></div>{error ? <p className="confirm-dialog__error" role="alert">{error.message}</p> : null}</section></div> : null}
  </div>;
}
