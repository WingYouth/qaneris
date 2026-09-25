import { useState } from "react";
import { ResultTable } from "../ask/ResultTable.jsx";
import { automaticChartKind, CHART_KIND, eligibleChartKinds, MAX_CHART_ROWS } from "../../visualization/chartPolicy.js";
import { buildChartSpec } from "../../visualization/chartSpec.js";
import { BarChart } from "./BarChart.jsx";
import { LineChart } from "./LineChart.jsx";
import { PieChart } from "./PieChart.jsx";
import { ChartSource } from "./ChartShared.jsx";

const LABELS = { auto: "自动", table: "表格", bar: "柱状图", line: "折线图", pie: "饼图" };

export function VisualizationPanel({ view }) {
  const [selected, setSelected] = useState("auto");
  if (view?.status !== "completed" || !view.table) return null;
  const eligible = eligibleChartKinds(view);
  const kind = selected === "auto" ? automaticChartKind(view) : selected;
  const spec = buildChartSpec(view, kind);
  const empty = view.table.rowCount === 0 || view.table.rows.length === 0;
  const tooMany = view.table.rows.length > MAX_CHART_ROWS;
  return <section className="visualization-panel" aria-label="结果视图">
    <div className="visualization-panel__header"><strong>结果视图</strong>
      <div className="chart-selector" role="group" aria-label="选择结果视图">
        {["auto", CHART_KIND.TABLE, CHART_KIND.BAR, CHART_KIND.LINE, CHART_KIND.PIE].map((option) =>
          <button key={option} type="button" className={selected === option ? "is-selected" : ""}
            aria-pressed={selected === option} disabled={option !== "auto" && !eligible.includes(option)}
            onClick={() => setSelected(option)}>{LABELS[option]}</button>)}
      </div>
    </div>
    {kind === CHART_KIND.TABLE || !spec ? <>
      <ResultTable table={view.table} />
      {empty ? <p className="chart-empty">当前查询没有可视化数据</p> : null}
      {tooMany ? <p className="chart-empty">Visualization unavailable · Too many rows for Roadshow chart view</p> : null}
    </> : <>
      {kind === CHART_KIND.BAR ? <BarChart spec={spec} /> : null}
      {kind === CHART_KIND.LINE ? <LineChart spec={spec} /> : null}
      {kind === CHART_KIND.PIE ? <PieChart spec={spec} /> : null}
      {spec.source.truncated ? <p className="chart-warning">结果已截断 · 图表仅基于当前返回数据</p> : null}
      <ChartSource source={spec.source} />
    </>}
  </section>;
}
