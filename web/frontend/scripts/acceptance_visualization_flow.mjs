/** Real HTTP/SSE → Ask View → chart policy/spec acceptance, with no duplicate Ask. */
import { streamAskQuestion } from "../src/api/askStream.js";
import { askViewFromResponse } from "../src/ask/askView.js";
import { automaticChartKind, eligibleChartKinds } from "../src/visualization/chartPolicy.js";
import { buildChartSpec, isChartSpec } from "../src/visualization/chartSpec.js";

export const QUESTIONS = {
  bar: "对 orders 表，按 region 分组汇总 amount 的总和。",
  line: "2026-01-05 至 2026-02-02，按 order_date 汇总 amount 的趋势。",
  scalar: "orders 表的 amount 总额是多少？",
  empty: "按 region 汇总 amount，筛选 region = North。",
};

function argument(name) {
  const index = process.argv.indexOf(`--${name}`);
  if (index < 0 || !process.argv[index + 1]) throw new Error(`--${name} is required`);
  return process.argv[index + 1];
}

async function ask(question, datasourceId) {
  let response = null;
  await streamAskQuestion({ question, datasourceId, workspaceId: "default", onEvent: (event) => {
    if (["result_ready", "clarification_required", "error"].includes(event.event_type)) {
      response = event.payload?.response || null;
    }
  } });
  if (!response) throw new Error("terminal Ask response missing");
  const view = askViewFromResponse(response);
  const auto = automaticChartKind(view);
  return { status: view.status, table: view.table, plan: view.plan,
    auto, eligible: eligibleChartKinds(view), autoSpec: buildChartSpec(view, auto),
    pieSpec: buildChartSpec(view, "pie") };
}

async function main() {
  const baseUrl = argument("base-url").replace(/\/$/, "");
  const datasourceId = argument("datasource-id");
  const nativeFetch = globalThis.fetch;
  globalThis.fetch = (input, init) => nativeFetch(typeof input === "string" ? new URL(input, baseUrl) : input, init);
  const cases = {};
  for (const [name, question] of Object.entries(QUESTIONS)) cases[name] = await ask(question, datasourceId);
  const bar = cases.bar.autoSpec;
  cases.invalidSpecRejected = !isChartSpec({ ...bar, kind: "scatter" })
    && !isChartSpec({ ...bar, points: [{ category: "x", values: { sum_amount: Infinity } }] });
  cases.unsupportedFallsBack = buildChartSpec({ status: "completed", table: cases.bar.table,
    plan: cases.bar.plan }, "scatter") === null;
  console.log(JSON.stringify(cases));
}

main().catch((error) => { console.error(error?.stack || String(error)); process.exit(1); });
