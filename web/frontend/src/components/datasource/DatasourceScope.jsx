/**
 * The Ask Scope selector.
 *
 * Only datasources the API reports as READY can be asked against, and the selected value is always a
 * datasource id the API returned - never a value typed by hand.
 */
export function DatasourceScope({ datasources, scope, onScopeChange, onRefresh }) {
  const ready = datasources.filter((datasource) => datasource?.status === "ready");

  return (
    <section className="surface-card scope-card" aria-labelledby="scope-title">
      <div className="foundation-card__header">
        <span className="step-label">问数范围</span>
        <button className="secondary-button" type="button" onClick={onRefresh}>
          刷新
        </button>
      </div>
      <h2 id="scope-title">选择数据源</h2>

      {datasources.length === 0 ? (
        <p className="scope-card__empty">
          当前 Workspace 尚无数据源。请添加数据源或导入 Excel 工作簿。
        </p>
      ) : (
        <label className="field">
          <span className="field__label">可问数的数据源</span>
          <select
            className="field__input"
            value={scope}
            onChange={(event) => onScopeChange(event.target.value)}
          >
            {datasources.map((datasource) => (
              <option
                key={datasource.id}
                value={datasource.id}
                disabled={datasource.status !== "ready"}
              >
                {datasource.name || datasource.id} · {datasource.status === "ready" ? "已扫描" : "待扫描"} · {datasource.id}
              </option>
            ))}
          </select>
        </label>
      )}

      <p className="scope-card__count">
        {ready.length} 个已扫描 / 共 {datasources.length} 个。连接状态可在数据源页检查。
      </p>
    </section>
  );
}
