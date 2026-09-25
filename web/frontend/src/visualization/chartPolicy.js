export const CHART_KIND = Object.freeze({ TABLE: "table", BAR: "bar", LINE: "line", PIE: "pie" });
export const MAX_CHART_ROWS = 50;

const categoryValue = (value) => value == null || ["string", "number", "boolean"].includes(typeof value);

export function analyzeTable(table, plan) {
  const columns = Array.isArray(table?.columns) ? table.columns : [];
  const rows = Array.isArray(table?.rows) ? table.rows : [];
  const valid = columns.length > 0 && table?.rowCount !== 0
    && rows.length >= 2 && rows.length <= MAX_CHART_ROWS;
  const numeric = columns.filter((field) => {
    const values = rows.map((row) => row?.[field]).filter((value) => value != null);
    return values.length > 0 && values.every((value) => typeof value === "number" && Number.isFinite(value));
  });
  const nonNumeric = columns.filter((field) => !numeric.includes(field)
    && rows.every((row) => categoryValue(row?.[field])));
  const preferred = plan?.groupBy?.[0];
  const category = nonNumeric.includes(preferred) ? preferred : nonNumeric[0] || null;
  const aliases = (plan?.aggregates || []).map((item) => item?.alias);
  const series = [...new Set([...aliases, ...columns])].filter((field) => numeric.includes(field) && field !== category).slice(0, 3);
  const timeField = plan?.timeField;
  const lineSeries = [...new Set([...aliases, ...columns])]
    .filter((field) => numeric.includes(field) && field !== timeField).slice(0, 3);
  const line = valid && Boolean(timeField) && columns.includes(timeField)
    && rows.every((row) => categoryValue(row?.[timeField])) && lineSeries.length > 0;
  const bar = valid && Boolean(category) && series.length > 0;
  const pieSeries = numeric.filter((field) => field !== category);
  const pie = bar && rows.length <= 8 && pieSeries.length === 1
    && rows.every((row) => typeof row?.[pieSeries[0]] === "number" && row[pieSeries[0]] >= 0)
    && rows.some((row) => row[pieSeries[0]] > 0)
    && Number.isFinite(rows.reduce((sum, row) => sum + row[pieSeries[0]], 0));
  return { valid, numeric, category, series, timeField: line ? timeField : null,
    lineSeries: lineSeries.slice(0, 3), bar, line, pie, pieSeries };
}

export function eligibleChartKinds(view) {
  const kinds = [CHART_KIND.TABLE];
  if (view?.status !== "completed") return kinds;
  const analysis = analyzeTable(view.table, view.plan);
  if (analysis.bar) kinds.push(CHART_KIND.BAR);
  if (analysis.line) kinds.push(CHART_KIND.LINE);
  if (analysis.pie) kinds.push(CHART_KIND.PIE);
  return kinds;
}

export function automaticChartKind(view) {
  const eligible = eligibleChartKinds(view);
  if (eligible.includes(CHART_KIND.LINE)) return CHART_KIND.LINE;
  if (eligible.includes(CHART_KIND.BAR)) return CHART_KIND.BAR;
  return CHART_KIND.TABLE;
}
