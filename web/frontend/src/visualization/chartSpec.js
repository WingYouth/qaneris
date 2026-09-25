import { analyzeTable, CHART_KIND, eligibleChartKinds } from "./chartPolicy.js";

export function buildChartSpec(view, kind) {
  if (![CHART_KIND.BAR, CHART_KIND.LINE, CHART_KIND.PIE].includes(kind)
    || !eligibleChartKinds(view).includes(kind)) return null;
  const analysis = analyzeTable(view.table, view.plan);
  const categoryField = kind === CHART_KIND.LINE ? analysis.timeField : analysis.category;
  const fields = kind === CHART_KIND.LINE ? analysis.lineSeries
    : kind === CHART_KIND.PIE ? analysis.pieSeries : analysis.series;
  const spec = {
    kind,
    category: { field: categoryField, label: categoryField },
    series: fields.map((field) => ({ field, label: field })),
    points: view.table.rows.map((row) => ({
      category: row?.[categoryField] == null ? "—" : String(row[categoryField]),
      values: Object.fromEntries(fields.map((field) => [field, row?.[field] ?? null])),
    })),
    source: {
      rowCount: view.table.rowCount,
      truncated: view.table.truncated === true || view.evidence?.truncated === true,
      datasourceId: view.evidence?.datasourceId || "",
      scanVersion: view.evidence?.scanVersion ?? null,
    },
  };
  return isChartSpec(spec) ? spec : null;
}

/** Validate the render boundary even though normal specs are produced locally. */
export function isChartSpec(spec) {
  if (!spec || ![CHART_KIND.BAR, CHART_KIND.LINE, CHART_KIND.PIE].includes(spec.kind)
    || typeof spec.category?.field !== "string" || !spec.category.field
    || !Array.isArray(spec.series) || spec.series.length < 1 || spec.series.length > 3
    || !spec.series.every((series) => typeof series?.field === "string" && series.field)
    || !Array.isArray(spec.points) || spec.points.length < 2 || spec.points.length > 50
    || !spec.source || !Number.isFinite(spec.source.rowCount)) return false;
  if (spec.kind === CHART_KIND.PIE && (spec.series.length !== 1 || spec.points.length > 8)) return false;
  const fieldNames = spec.series.map((series) => series.field);
  if (new Set(fieldNames).size !== fieldNames.length) return false;
  if (!spec.points.every((point) => typeof point?.category === "string"
    && point.values && typeof point.values === "object" && !Array.isArray(point.values)
    && fieldNames.every((field) => {
      const value = point.values[field];
      return value === null && spec.kind !== CHART_KIND.PIE
        || typeof value === "number" && Number.isFinite(value)
          && (spec.kind !== CHART_KIND.PIE || value >= 0);
    }))) return false;
  if (spec.kind === CHART_KIND.PIE) {
    const sum = spec.points.reduce((total, point) => total + point.values[fieldNames[0]], 0);
    return Number.isFinite(sum) && sum > 0;
  }
  return true;
}
