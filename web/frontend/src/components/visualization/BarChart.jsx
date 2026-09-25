import { formatNumber, linearScale, numericDomain } from "../../visualization/chartMath.js";
import { ChartLegend, chartSummary, shortLabel, ValueTitle } from "./ChartShared.jsx";

export function BarChart({ spec }) {
  const width = 720; const height = 300; const left = 72; const right = 20; const top = 20; const bottom = 72;
  const plotWidth = width - left - right; const plotHeight = height - top - bottom;
  const values = spec.points.flatMap((point) => spec.series.map((item) => point.values[item.field]));
  const domain = numericDomain(values, true);
  const y = linearScale(domain, [top + plotHeight, top]);
  const zero = y(0); const groupWidth = plotWidth / spec.points.length;
  const barWidth = Math.min(34, groupWidth * 0.75 / spec.series.length);
  const summary = chartSummary(spec, "柱状图");
  return <figure className="chart"><svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={summary}>
    <line x1={left} x2={width - right} y1={zero} y2={zero} className="chart-axis" />
    <text x={left - 8} y={Math.max(top + 12, Math.min(height - bottom, zero - 4))} textAnchor="end" className="chart-tick">0</text>
    {spec.points.map((point, index) => {
      const center = left + (index + 0.5) * groupWidth;
      return <g key={index}>
        {spec.series.map((item, seriesIndex) => {
          const value = point.values[item.field];
          if (value == null) return null;
          const x = center + (seriesIndex - (spec.series.length - 1) / 2) * barWidth - barWidth * 0.45;
          const valueY = y(value);
          return <rect key={item.field} x={x} y={Math.min(zero, valueY)} width={barWidth * 0.9}
            height={Math.abs(zero - valueY)} className={`chart-fill-${seriesIndex % 5}`}>
            <ValueTitle category={point.category} value={value} />
          </rect>;
        })}
        {index % Math.ceil(spec.points.length / 8) === 0 ? <text x={center} y={height - bottom + 20}
          textAnchor="middle" className="chart-tick"><title>{point.category}</title>{shortLabel(point.category)}</text> : null}
      </g>;
    })}
    <text x={left - 8} y={top + 9} textAnchor="end" className="chart-tick">{formatNumber.format(domain[1])}</text>
    <text x={left - 8} y={top + plotHeight} textAnchor="end" className="chart-tick">{formatNumber.format(domain[0])}</text>
  </svg><figcaption>{summary}</figcaption><ChartLegend series={spec.series} /></figure>;
}
