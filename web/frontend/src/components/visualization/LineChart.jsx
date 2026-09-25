import { formatNumber, linearScale, lineSegments, numericDomain } from "../../visualization/chartMath.js";
import { ChartLegend, chartSummary, shortLabel, ValueTitle } from "./ChartShared.jsx";

export function LineChart({ spec }) {
  const width = 720; const height = 300; const left = 72; const right = 20; const top = 20; const bottom = 72;
  const domain = numericDomain(spec.points.flatMap((point) => spec.series.map((item) => point.values[item.field])));
  const y = linearScale(domain, [height - bottom, top]);
  const x = (index) => left + index * (width - left - right) / (spec.points.length - 1);
  const summary = chartSummary(spec, "折线图");
  return <figure className="chart"><svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={summary}>
    <line x1={left} x2={width - right} y1={height - bottom} y2={height - bottom} className="chart-axis" />
    {spec.series.map((item, seriesIndex) => lineSegments(spec.points, item.field).map((segment, groupIndex) =>
      <g key={`${item.field}-${groupIndex}`}>
        {segment.length > 1 ? <polyline points={segment.map((point) => `${x(point.index)},${y(point.value)}`).join(" ")}
          className={`chart-line-${seriesIndex % 5}`} /> : null}
        {segment.map((point) => <circle key={point.index} cx={x(point.index)} cy={y(point.value)} r="4"
          className={`chart-fill-${seriesIndex % 5}`}><ValueTitle category={point.category} value={point.value} /></circle>)}
      </g>))}
    {spec.points.map((point, index) => index % Math.ceil(spec.points.length / 8) === 0
      ? <text key={index} x={x(index)} y={height - bottom + 20} textAnchor="middle" className="chart-tick">
        <title>{point.category}</title>{shortLabel(point.category)}</text> : null)}
    <text x={left - 8} y={top + 9} textAnchor="end" className="chart-tick">{formatNumber.format(domain[1])}</text>
    <text x={left - 8} y={height - bottom} textAnchor="end" className="chart-tick">{formatNumber.format(domain[0])}</text>
  </svg><figcaption>{summary}</figcaption><ChartLegend series={spec.series} /></figure>;
}
