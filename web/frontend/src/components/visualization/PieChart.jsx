import { formatNumber, pieSlices } from "../../visualization/chartMath.js";
import { chartSummary } from "./ChartShared.jsx";

function arcPath(slice) {
  const cx = 150; const cy = 150; const r = 118;
  const point = (angle) => [cx + r * Math.sin(angle), cy - r * Math.cos(angle)];
  if (slice.fraction === 1) {
    return `M ${cx} ${cy - r} A ${r} ${r} 0 1 1 ${cx} ${cy + r} A ${r} ${r} 0 1 1 ${cx} ${cy - r} Z`;
  }
  const [sx, sy] = point(slice.start); const [ex, ey] = point(slice.end);
  return `M ${cx} ${cy} L ${sx} ${sy} A ${r} ${r} 0 ${slice.fraction > 0.5 ? 1 : 0} 1 ${ex} ${ey} Z`;
}

export function PieChart({ spec }) {
  const field = spec.series[0].field;
  const slices = pieSlices(spec.points.map((point) => point.values[field]));
  const summary = chartSummary(spec, "饼图");
  return <figure className="chart chart--pie"><svg viewBox="0 0 300 300" role="img" aria-label={summary}>
    {slices.map((slice, index) => slice.value > 0 ? <path key={index} d={arcPath(slice)} className={`chart-fill-${index % 5}`}>
      <title>{spec.points[index].category}：{formatNumber.format(slice.value)}（{formatNumber.format(slice.fraction * 100)}%）</title>
    </path> : null)}
  </svg><figcaption>{summary}</figcaption><ul className="chart-legend chart-legend--pie">
    {slices.map((slice, index) => <li key={index}><span className={`chart-swatch chart-color-${index % 5}`} />
      <span>{spec.points[index].category}</span><strong>{formatNumber.format(slice.value)}</strong>
      <span>{formatNumber.format(slice.fraction * 100)}%</span></li>)}
  </ul></figure>;
}
