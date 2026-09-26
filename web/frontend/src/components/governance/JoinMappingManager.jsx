import { useCallback, useEffect, useMemo, useState } from "react";
import {
  confirmJoinMapping,
  createJoinMapping,
  listJoinMappings,
  listPublishedStructure,
  rejectJoinMapping,
} from "../../api/joinMappings.js";
import {
  confirmFailureMessage,
  createFailureMessage,
  datasourcePairs,
  fieldsFor,
  mappingSummary,
  needingAttention,
  objectsFor,
  STATUS_LABEL,
  suggestedKeys,
} from "../../governance/joinMappingView.js";

const CARDINALITIES = [
  ["MANY_TO_ONE", "多对一", "多行订单对应一行客户"],
  ["ONE_TO_ONE", "一对一", "两侧键都唯一"],
  ["ONE_TO_MANY", "一对多", "一行对应多行"],
];

const NULL_POLICIES = [
  ["REJECT", "丢弃匹配不上的行", "只保留两侧都能匹配的数据"],
  ["DROP", "保留未匹配的行", "未匹配一侧留空"],
];

const emptyForm = (pair) => ({
  pairKey: pair?.key || "",
  businessKey: "",
  leftObjectId: "",
  leftFieldPath: "",
  leftGrain: "",
  rightObjectId: "",
  rightFieldPath: "",
  rightGrain: "",
  cardinality: "MANY_TO_ONE",
  nullPolicy: "REJECT",
});

/**
 * The Join Mapping workspace.
 *
 * A cross-source `keyed_join` is refused by the plan validator until a mapping is CONFIRMED, and
 * confirmation resolves both sides against the published graph. This screen is the only place that
 * can do that, which is why it shows the graph node ids from the backend rather than asking a user
 * to supply them.
 */
export function JoinMappingManager({ workspaceId = "default", backendAvailable = true }) {
  const [objects, setObjects] = useState([]);
  const [mappings, setMappings] = useState([]);
  const [truncated, setTruncated] = useState(false);
  const [form, setForm] = useState(emptyForm());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState("");
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const [structure, listed] = await Promise.all([
        listPublishedStructure(workspaceId),
        listJoinMappings(workspaceId),
      ]);
      setObjects(structure.objects || []);
      setTruncated(Boolean(structure.truncated));
      setMappings(listed || []);
      setError(null);
    } catch (failure) {
      setError(failure);
      setObjects([]);
      setMappings([]);
    } finally {
      setLoading(false);
    }
  }, [workspaceId]);

  useEffect(() => { refresh(); }, [refresh]);

  const pairs = useMemo(() => datasourcePairs(objects), [objects]);
  const activePair = pairs.find((pair) => pair.key === form.pairKey) || pairs[0] || null;

  useEffect(() => {
    if (activePair && activePair.key !== form.pairKey) {
      setForm((current) => emptyForm(activePair));
    }
  }, [activePair, form.pairKey]);

  const leftObjects = activePair ? objectsFor(objects, activePair.left.id) : [];
  const rightObjects = activePair ? objectsFor(objects, activePair.right.id) : [];
  const leftFields = fieldsFor(objects, activePair?.left.id, form.leftObjectId);
  const rightFields = fieldsFor(objects, activePair?.right.id, form.rightObjectId);
  const suggestions = leftFields.length && rightFields.length
    ? suggestedKeys(leftFields, rightFields)
    : [];

  const canSubmit = Boolean(
    backendAvailable && activePair && form.businessKey.trim()
    && form.leftObjectId && form.leftFieldPath.trim() && form.leftGrain.trim()
    && form.rightObjectId && form.rightFieldPath.trim() && form.rightGrain.trim()
    && !busy
  );

  const changePair = (key) => {
    const next = pairs.find((pair) => pair.key === key);
    setNotice("");
    setForm(emptyForm(next));
  };

  const applySuggestion = (suggestion) => {
    setForm((current) => ({
      ...current,
      leftFieldPath: suggestion.left.path,
      rightFieldPath: suggestion.right.path,
    }));
  };

  const submit = async (event) => {
    event.preventDefault();
    if (!canSubmit) return;
    setBusy(true); setNotice(""); setError(null);
    try {
      await createJoinMapping({
        workspace_id: workspaceId,
        business_key: form.businessKey.trim(),
        left_datasource_id: activePair.left.id,
        left_data_object_id: form.leftObjectId,
        left_field_path: form.leftFieldPath.trim(),
        left_grain: form.leftGrain.trim(),
        right_datasource_id: activePair.right.id,
        right_data_object_id: form.rightObjectId,
        right_field_path: form.rightFieldPath.trim(),
        right_grain: form.rightGrain.trim(),
        cardinality: form.cardinality,
        null_policy: form.nullPolicy,
      });
      setForm(emptyForm(activePair));
      await refresh();
      setNotice("关联映射已登记为候选。确认后跨数据源关联才会生效。");
    } catch (failure) {
      setError({ message: createFailureMessage(failure), code: failure?.code });
    } finally {
      setBusy(false);
    }
  };

  const confirm = async (mapping) => {
    setBusy(true); setNotice(""); setError(null);
    try {
      await confirmJoinMapping(mapping.mapping_id, "web-workspace");
      await refresh();
      setNotice("关联映射已确认，现在可以执行跨数据源关联。");
    } catch (failure) {
      setError({ message: confirmFailureMessage(failure), code: failure?.code });
    } finally {
      setBusy(false);
    }
  };

  const reject = async (mapping) => {
    setBusy(true); setNotice(""); setError(null);
    try {
      await rejectJoinMapping(mapping.mapping_id);
      await refresh();
      setNotice("关联映射已拒绝，不会被联合分析使用。");
    } catch (failure) {
      setError({ message: failure?.message || "无法拒绝该关联映射。", code: failure?.code });
    } finally {
      setBusy(false);
    }
  };

  const pending = needingAttention(mappings);

  return <div className="governance-layout">
    <section className="surface-card governance-panel" aria-labelledby="join-heading">
      <div className="section-heading">
        <div>
          <span className="eyebrow">GOVERNANCE</span>
          <h2 id="join-heading">关联映射</h2>
          <p>跨数据源按键关联需要一条已确认的关联键；同名字段不会被自动视为关联。</p>
        </div>
        <button className="secondary-button" type="button" onClick={refresh} disabled={busy || loading}>
          {loading ? "读取中…" : "刷新"}
        </button>
      </div>

      {error ? <div className="feedback feedback--error" role="alert">
        <strong>操作未完成</strong><p>{error.message}</p>
      </div> : null}
      {notice ? <p className="source-notice" role="status">{notice}</p> : null}
      {truncated ? <p className="chart-warning">已发布结构超过读取上限，列表可能未列全。</p> : null}

      {pairs.length === 0 ? <div className="source-empty">
        <strong>还没有可关联的数据源组合</strong>
        <p>跨数据源关联需要两个及以上已扫描的数据源。请先在“数据源”页面添加并扫描。</p>
      </div> : <>
        <form className="join-form" onSubmit={submit}>
          <label className="field">
            <span className="field__label">数据源组合</span>
            <select className="field__input" value={form.pairKey || activePair?.key || ""}
              onChange={(event) => changePair(event.target.value)}>
              {pairs.map((pair) => <option key={pair.key} value={pair.key}>
                {pair.left.name} ↔ {pair.right.name}
              </option>)}
            </select>
          </label>

          <label className="field">
            <span className="field__label">业务键名称</span>
            <input className="field__input" value={form.businessKey} placeholder="例如：客户ID"
              onChange={(event) => setForm({ ...form, businessKey: event.target.value })} />
            <small className="field__hint">会记录在映射与证据里，用于说明两侧按什么关联。</small>
          </label>

          <div className="join-sides">
            {[["left", activePair?.left, leftObjects, leftFields], ["right", activePair?.right, rightObjects, rightFields]]
              .map(([side, source, sideObjects, sideFields]) => {
                const objectId = side === "left" ? form.leftObjectId : form.rightObjectId;
                const setObjectId = (value) => setForm({ ...form,
                  [side === "left" ? "leftObjectId" : "rightObjectId"]: value,
                  [side === "left" ? "leftFieldPath" : "rightFieldPath"]: "" });
                return <fieldset className="join-side" key={side}>
                  <legend>{source?.name || "数据源"}</legend>
                  <label className="field">
                    <span className="field__label">数据对象</span>
                    <select className="field__input" value={objectId} onChange={(event) => setObjectId(event.target.value)}>
                      <option value="">请选择</option>
                      {sideObjects.map((item) => <option key={item.node_id} value={item.node_id}>
                        {item.qualified_name || item.name}
                      </option>)}
                    </select>
                  </label>
                  <label className="field">
                    <span className="field__label">关联字段</span>
                    <select className="field__input" value={side === "left" ? form.leftFieldPath : form.rightFieldPath}
                      disabled={!objectId}
                      onChange={(event) => setForm({ ...form,
                        [side === "left" ? "leftFieldPath" : "rightFieldPath"]: event.target.value })}>
                      <option value="">请选择</option>
                      {sideFields.map((field) => <option key={field.node_id} value={field.path}>
                        {field.path}{field.data_type ? ` · ${field.data_type}` : ""}
                      </option>)}
                    </select>
                  </label>
                  <label className="field">
                    <span className="field__label">粒度</span>
                    <input className="field__input" value={side === "left" ? form.leftGrain : form.rightGrain}
                      placeholder="例如：一行订单" disabled={!objectId}
                      onChange={(event) => setForm({ ...form,
                        [side === "left" ? "leftGrain" : "rightGrain"]: event.target.value })} />
                    <small className="field__hint">这一侧一行代表什么，用于校验基数。</small>
                  </label>
                </fieldset>;
              })}
          </div>

          {suggestions.length ? <div className="join-suggestions">
            <span className="field__label">同名候选（仅供参考，不会自动关联）</span>
            <div className="join-suggestions__items">
              {suggestions.slice(0, 6).map((suggestion) => <button type="button" key={`${suggestion.left.node_id}-${suggestion.right.node_id}`}
                onClick={() => applySuggestion(suggestion)} disabled={busy}>
                {suggestion.left.path} ↔ {suggestion.right.path}
              </button>)}
            </div>
          </div> : null}

          <div className="join-policies">
            <label className="field">
              <span className="field__label">基数</span>
              <select className="field__input" value={form.cardinality}
                onChange={(event) => setForm({ ...form, cardinality: event.target.value })}>
                {CARDINALITIES.map(([value, label, hint]) => <option key={value} value={value}>{label} · {hint}</option>)}
              </select>
            </label>
            <label className="field">
              <span className="field__label">空值策略</span>
              <select className="field__input" value={form.nullPolicy}
                onChange={(event) => setForm({ ...form, nullPolicy: event.target.value })}>
                {NULL_POLICIES.map(([value, label, hint]) => <option key={value} value={value}>{label} · {hint}</option>)}
              </select>
            </label>
          </div>

          <button className="primary-button" type="submit" disabled={!canSubmit}>
            {busy ? "提交中…" : "登记为候选"}
          </button>
          {!backendAvailable ? <small className="field__hint">后端不可用，暂时无法登记。</small> : null}
        </form>
      </>}
    </section>

    <section className="surface-card governance-list" aria-labelledby="mapping-list-heading">
      <div className="section-heading">
        <div>
          <span className="eyebrow">MAPPINGS</span>
          <h2 id="mapping-list-heading">已登记的关联键</h2>
          <p>{mappings.length} 条记录 · {pending.length} 条待处理。只有已确认的映射会参与联合分析。</p>
        </div>
      </div>

      {mappings.length === 0 ? <div className="source-empty">
        <strong>还没有关联映射</strong>
        <p>左侧登记一条候选，再确认它，跨数据源按键关联就可用。</p>
      </div> : <div className="mapping-list">
        {mappings.map((mapping) => <article className="mapping-item" key={mapping.mapping_id}>
          <div className="mapping-item__head">
            <strong>{mapping.business_key}</strong>
            <span className={`mapping-status mapping-status--${mapping.status.toLowerCase()}`}>
              {STATUS_LABEL[mapping.status] || mapping.status}
            </span>
          </div>
          <p className="mapping-item__pair">{mappingSummary(mapping)}</p>
          <dl className="mapping-item__meta">
            <div><dt>左侧</dt><dd>{mapping.left_datasource_id} · {mapping.left_grain}</dd></div>
            <div><dt>右侧</dt><dd>{mapping.right_datasource_id} · {mapping.right_grain}</dd></div>
            <div><dt>基数</dt><dd>{mapping.cardinality}</dd></div>
            <div><dt>空值</dt><dd>{mapping.null_policy}</dd></div>
            {mapping.left_scan_version != null ? <div><dt>扫描版本</dt><dd>{mapping.left_scan_version} / {mapping.right_scan_version}</dd></div> : null}
            {mapping.confirmed_by ? <div><dt>确认人</dt><dd>{mapping.confirmed_by}</dd></div> : null}
          </dl>
          {mapping.status === "CANDIDATE" || mapping.status === "STALE" ? <div className="mapping-item__actions">
            <button className="primary-button" type="button" disabled={busy} onClick={() => confirm(mapping)}>确认</button>
            <button className="secondary-button" type="button" disabled={busy} onClick={() => reject(mapping)}>拒绝</button>
          </div> : null}
          {mapping.status === "STALE" ? <p className="chart-warning">
            绑定的扫描版本已变化，需要重新确认后才会再次生效。
          </p> : null}
        </article>)}
      </div>}
    </section>
  </div>;
}
