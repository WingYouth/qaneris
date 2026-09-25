import { useMemo, useState } from "react";

const DRIVER_NAMES = {
  postgresql: "PostgreSQL", mysql: "MySQL", sqlserver: "SQL Server", sqlite: "SQLite",
  oracle: "Oracle", timescaledb: "TimescaleDB", clickhouse: "ClickHouse",
  mongodb: "MongoDB", couchdb: "CouchDB", redis: "Redis", cassandra: "Cassandra",
  hbase: "HBase", elasticsearch: "Elasticsearch", opensearch: "OpenSearch",
  influxdb: "InfluxDB", neo4j: "Neo4j", milvus: "Milvus", qdrant: "Qdrant",
  weaviate: "Weaviate", snowflake: "Snowflake", bigquery: "BigQuery",
};
const POPULAR = ["postgresql", "mysql", "sqlserver", "sqlite", "clickhouse", "timescaledb", "mongodb", "elasticsearch"];
const GROUPS = [
  { id: "all", label: "全部数据库" },
  { id: "popular", label: "常用" },
  { id: "sql", label: "关系型 SQL", drivers: ["postgresql", "mysql", "sqlserver", "sqlite", "oracle"] },
  { id: "document", label: "文档 / KV", drivers: ["mongodb", "couchdb", "redis", "cassandra", "hbase"] },
  { id: "analytics", label: "分析与时序", drivers: ["clickhouse", "timescaledb", "influxdb", "snowflake", "bigquery"] },
  { id: "search", label: "搜索引擎", drivers: ["elasticsearch", "opensearch"] },
  { id: "graph", label: "图与向量", drivers: ["neo4j", "milvus", "qdrant", "weaviate"] },
];

export const driverName = (driver) => DRIVER_NAMES[driver] || driver || "数据库";

export function DriverPicker({ adapters, selected, onSelect, onNext, onCancel }) {
  const [query, setQuery] = useState("");
  const [group, setGroup] = useState("all");
  const visible = useMemo(() => {
    const category = GROUPS.find((item) => item.id === group);
    const needle = query.trim().toLocaleLowerCase();
    return adapters.filter(({ driver }) => {
      const inGroup = group === "all" || (group === "popular" ? POPULAR.includes(driver) : category?.drivers?.includes(driver));
      return inGroup && (!needle || `${driverName(driver)} ${driver}`.toLocaleLowerCase().includes(needle));
    }).sort((left, right) => {
      const leftRank = POPULAR.indexOf(left.driver);
      const rightRank = POPULAR.indexOf(right.driver);
      return (leftRank < 0 ? 100 : leftRank) - (rightRank < 0 ? 100 : rightRank) || driverName(left.driver).localeCompare(driverName(right.driver));
    });
  }, [adapters, group, query]);

  return <div className="connection-flow">
    <div className="connection-flow__back"><button type="button" onClick={onCancel}>← 返回数据源</button></div>
    <header className="connection-flow__heading"><div><h1>选择数据库</h1><p>选择驱动后，填写连接信息并测试。支持的数据库按用途分类展示。</p></div><span>01 / 03 · 选择类型</span></header>
    <div className="surface-card driver-picker">
      <nav className="driver-picker__groups" aria-label="数据库分类"><span className="eyebrow">分类</span>{GROUPS.map((item) => <button key={item.id} type="button" className={group === item.id ? "is-selected" : ""} aria-pressed={group === item.id} onClick={() => setGroup(item.id)}>{item.label}</button>)}</nav>
      <div className="driver-picker__main"><div className="driver-picker__toolbar"><label className="driver-picker__search"><span className="sr-only">搜索数据库或驱动名称</span><input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索数据库或驱动名称" /></label><span>{adapters.length} 种驱动</span></div>
        <div className="driver-picker__section-label">{group === "all" ? "可用数据库" : GROUPS.find((item) => item.id === group)?.label}</div>
        {visible.length ? <div className="driver-picker__grid">{visible.map(({ driver, kind }) => <button key={driver} type="button" className={`driver-tile ${selected === driver ? "is-selected" : ""}`} aria-pressed={selected === driver} onClick={() => onSelect(driver)}><span className="driver-tile__mark" aria-hidden="true">{driverName(driver).replace(/[^A-Za-z]/g, "").slice(0, 2).toUpperCase()}</span><strong>{driverName(driver)}</strong><small>{kind.replaceAll("_", " ")}</small>{selected === driver ? <span className="driver-tile__check" aria-hidden="true">✓</span> : null}</button>)}</div> : <p className="driver-picker__empty">没有匹配的驱动。请尝试其他关键词或分类。</p>}
        <div className="driver-picker__selection">{selected ? <>当前选择 <strong>{driverName(selected)}</strong> · {adapters.find((item) => item.driver === selected)?.kind}</> : "请选择一种数据库"}<span>驱动列表由后端提供</span></div>
      </div>
    </div>
    <footer className="connection-flow__footer"><p>连接测试将由 SmartData 后端执行。</p><div><button type="button" className="connection-flow__button" onClick={onCancel}>取消</button><button type="button" className="primary-button" disabled={!selected} onClick={onNext}>下一步：配置连接 <span aria-hidden="true">→</span></button></div></footer>
  </div>;
}
