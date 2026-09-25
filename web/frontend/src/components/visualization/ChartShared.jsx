import { formatNumber } from "../../visualization/chartMath.js";

export function shortLabel(value) {
  return value.length > 15 ? `${value.slice(0, 14)}…` : value;
}

export function ChartLegend({ series }) {
  if (series.length < 2) return null;
  return <ul className="chart-legend">{series.map((item, index) =>
    <li key={item.field}><span className={`chart-swatch chart-color-${index % 5}`} />{item.label}</li>)}</ul>;
}

export function ChartSource({ source }) {
  return <div className="chart-source">
    <span>{source.rowCount} rows</span>
    {source.datasourceId ? <span>Datasource {source.datasourceId}</span> : null}
    {source.scanVersion != null ? <span>Scan version {source.scanVersion}</span> : null}
  </div>;
}

export function chartSummary(spec, name) {
  return `${name}：${spec.category.label} 对 ${spec.series.map((item) => item.label).join("、")}，共 ${spec.points.length} 个数据点。`;
}

export function ValueTitle({ category, value }) {
  return <title>{category}：{formatNumber.format(value)}</title>;
}
