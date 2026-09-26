import { useState } from "react";
import { conversationTitle } from "../../conversation/conversationState.js";

/** A stable, human label for a driver id. Unknown drivers fall back to a capitalised id. */
const DRIVER_LABELS = {
  sqlserver: "SQL server", postgresql: "PostgreSQL", timescaledb: "TimescaleDB", clickhouse: "ClickHouse",
  couchdb: "CouchDB", mongodb: "MongoDB", mysql: "MySQL", neo4j: "Neo4j", influxdb: "InfluxDB",
  elasticsearch: "Elasticsearch", opensearch: "OpenSearch", milvus: "Milvus", qdrant: "Qdrant",
  weaviate: "Weaviate", cassandra: "Cassandra", redis: "Redis", sqlite: "SQLite", oracle: "Oracle",
  hbase: "HBase", snowflake: "Snowflake", bigquery: "BigQuery",
};
export const driverLabel = (driver) => DRIVER_LABELS[driver] || `${driver.charAt(0).toUpperCase()}${driver.slice(1)}`;

const clock = (value) => {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
};
const stamp = (value) => {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toDateString() === new Date().toDateString() ? clock(value) : `${date.getFullYear()}/${date.getMonth() + 1}/${date.getDate()}`;
};
const scopeSummary = (conversation) => {
  const scoped = conversation?.datasource_scope || [];
  return scoped.length ? `${scoped.length} 个指定数据源` : "工作区自动选择";
};

/**
 * Conversation history plus the Ask scope.
 *
 * The scope is always a datasource id the API returned, never a typed value, and only a READY
 * datasource can be selected. When the workspace has no datasource yet the panel lists the driver
 * kinds the backend actually supports, disabled, so the empty state still says what can be added.
 */
export function ConversationSidebar({
  conversations, selectedId, onSelect, onCreate, creating, disabled,
  datasources, scope, onScopeChange, drivers = [], query = "", workspaceId = "default", onWorkspaceChange, onAddDatasource,
}) {
  const [filter, setFilter] = useState("");
  const needle = (filter.trim() || query.trim()).toLowerCase();
  const matches = (text) => !needle || String(text || "").toLowerCase().includes(needle);
  const shownConversations = conversations.filter((item) => matches(conversationTitle(item)));
  const ready = datasources.filter((item) => item.status === "ready");
  const shownSources = ready.filter((item) => matches(item.name || item.id));
  const workspaces = [...new Set([workspaceId, ...datasources.map((item) => item.workspace_id).filter(Boolean)])];
  return <aside className="conversation-sidebar" aria-label="对话历史">
    <div className="conversation-sidebar__heading"><strong>对话历史</strong><small>{conversations.length} 个对话</small></div>
    <button className="primary-button conversation-new" type="button" onClick={onCreate} disabled={creating || disabled}>+ 新对话</button>
    <nav className="conversation-list" aria-label="已有对话">
      {shownConversations.map((item) => <button key={item.conversation_id} type="button"
        aria-current={selectedId === item.conversation_id ? "page" : undefined}
        className={selectedId === item.conversation_id ? "is-selected" : ""}
        onClick={() => onSelect(item.conversation_id)}>
        <strong>{conversationTitle(item)}</strong>
        <small>{scopeSummary(item)}</small>
        <time dateTime={item.updated_at || undefined}>{stamp(item.updated_at)}</time>
      </button>)}
      {!shownConversations.length ? <p className="conversation-list__empty">{conversations.length ? "没有匹配的对话。" : "还没有对话，先新建一个。"}</p> : null}
    </nav>
    <section className="scope-panel" aria-labelledby="scope-panel-title">
      <div className="scope-panel__head"><strong id="scope-panel-title">数据源选择</strong>
        <label className="sr-only" htmlFor="scope-workspace">工作区</label>
        <select id="scope-workspace" value={workspaceId} onChange={(event) => onWorkspaceChange?.(event.target.value)} disabled={!onWorkspaceChange}>
          {workspaces.map((item) => <option key={item} value={item}>{item === workspaceId ? "全部项目" : item}</option>)}
        </select>
      </div>
      <label className="scope-search">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true"><circle cx="11" cy="11" r="6.5" /><path d="m16 16 4.5 4.5" /></svg>
        <input value={filter} onChange={(event) => setFilter(event.target.value)} placeholder="搜索数据源..." aria-label="搜索数据源" />
      </label>
      <div className="scope-list">
        <label className="scope-row scope-row--auto">
          <input type="radio" name="ask-scope" checked={scope.length === 0} onChange={() => onScopeChange([])} />
          <span>工作区自动选择</span><em>推荐</em>
        </label>
        {shownSources.map((item) => <label className="scope-row" key={item.id}>
          <input type="checkbox" checked={scope.includes(item.id)} disabled={disabled}
            onChange={(event) => onScopeChange(event.target.checked ? [...scope, item.id] : scope.filter((id) => id !== item.id))} />
          <span>{item.name || item.id}</span>
        </label>)}
        {!ready.length ? drivers.map((driver) => <label className="scope-row scope-row--muted" key={driver}>
          <input type="checkbox" disabled /><span>{driverLabel(driver)}</span>
        </label>) : null}
        {ready.length && !shownSources.length ? <p className="conversation-list__empty">没有匹配的数据源。</p> : null}
      </div>
      <button className="scope-add" type="button" onClick={onAddDatasource} disabled={!onAddDatasource}>+ 添加数据源</button>
    </section>
  </aside>;
}
