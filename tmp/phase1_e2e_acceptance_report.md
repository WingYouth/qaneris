# Phase 1 Real LLM E2E 验收报告

> **本报告的来源需要说明清楚**：它由**真实运行留下的持久记录**整理而成
> （见文末「记录来源」），**不是**现场重跑的产物。
> 现场重跑当前不可行——模型端点已返回 `429 insufficient balance`（见「当前环境状态」）。
>
> 逐题证据里凡是**当时打印并被记录**的都原样引用；当时未记录、且原始日志已随
> `/tmp` 清理丢失的字段（如每题的 `plan_id` / `scan_version`），报告中**明确标注为未保留**，
> 不做任何补写。

## 1. 结论

| 项 | 结果 |
| --- | --- |
| Phase 1 Real LLM E2E Gate | **通过**（六类问题共 18 个验收轮次，18/18 符合预期）|
| 真实调用大模型 | **是**（真实 OpenAI 兼容端点，非 mock）|
| 真实 Neo4j 检索 / 落地 | **是** |
| 真实 SQLite 执行 | **是** |
| 业务值 SQL 拼接 | **无**（业务值只在参数元组里）|
| 物理事实重新猜测 | **无**（时间轴与 join 都来自图确认绑定）|

## 2. 环境（不含任何凭据）

| 项 | 值 |
| --- | --- |
| 大模型 | 真实 OpenAI 兼容端点，主机 `api-inference.modelscope.cn`，路径 `/v1`，模型 `deepseek-ai/DeepSeek-V4-Pro-0813` |
| 调用链 | `ask()` → `IntentUnderstandingPipeline` → `LLMBusinessParser` → `httpx POST {base_url}/chat/completions` |
| 采样参数 | `temperature=0`（`llm/gateway.py`），`response_format=json_object` |
| 图数据库 | 真实 Neo4j，`bolt://localhost:7687`，镜像 `neo4j:2026.08.1` |
| 业务库 | 真实 SQLite 文件（见第 3 节结构）|
| 凭据 | 只从环境变量 / `.env` 读取；`.env` 已在 `.gitignore`，未写入仓库、测试、日志、Evidence 或文档 |

**「确实调用了大模型」的三条旁证**：

1. `SMARTDATA_MODEL_BASE_URL` 原先缺 `/v1` 时，调用返回 **404**；补上 `/v1` 后返回 **200**。这
   证明请求真的打到了该端点的 `/chat/completions`，而不是走了本地兜底。
2. 直接探测该端点时，响应 `message` 里带有 `reasoning_content`（推理内容）与
   `function_calls` / `tool_calls` 字段——这是推理型模型返回的真实响应结构。
3. 该密钥额度随后被耗尽：端点现在返回 `{"error":{"message":"insufficient balance"}}`。
   额度被消耗本身就是真实调用的直接证据。

## 3. 验收 scope

由 `smartdata/scripts/acceptance/phase1_e2e_scope.py` 幂等构建，产物落在
`SmartDataArtifacts/acceptance/phase1/`（`artifact_root()` 强制在项目目录之外）。

SQLite 源库（日期锚定运行当日，保证相对时间表达可解析）：

```sql
CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, region TEXT NOT NULL);
CREATE TABLE orders (
    id INTEGER PRIMARY KEY, order_no TEXT NOT NULL, customer_id INTEGER NOT NULL,
    order_date DATE NOT NULL, delivery_region TEXT NOT NULL,
    amount REAL NOT NULL, total_amount REAL NOT NULL,
    FOREIGN KEY(customer_id) REFERENCES customers(id)
);
CREATE INDEX idx_orders_customer ON orders(customer_id);
CREATE INDEX idx_orders_date ON orders(order_date);
```

规模：customers 42 行 / orders 360 行，14 个地区，日期覆盖最近 45 天。

图发布校验（全部 PASS）：`:Database` 节点已发布；DataObject == 2；外键成为
**已确认 `RELATES_TO`**（`orders.customer_id → customers.id`，`source=database`、`confirmed=true`）；
**存在真正 DATE 型字段** `orders.order_date`。

受治理语义资产 9 个（`phase1_e2e_assets.json`，经 `SemanticAssetBootstrap(graph_reader=...)`
解析出图物理身份）：

| asset_id | kind | 绑定 |
| --- | --- | --- |
| `phase1.metric.paid_sales` | metric（sum）| orders.amount |
| `phase1.metric.orders_count` | metric（count）| orders.id |
| `phase1.metric.turnover` | metric（sum）| orders.amount |
| `phase1.metric.turnover_gross` | metric（sum）| orders.total_amount（与上一个**共享别名「营业额」**，构成真实歧义对）|
| `phase1.dimension.delivery_region` | dimension | orders.delivery_region |
| `phase1.dimension.customer_region` | dimension | customers.region（跨对象，需真实 join）|
| `phase1.dimension.order_date` | dimension（**`time_axis`**）| orders.order_date |
| `phase1.term.order` | business_term | DataObject orders |
| `phase1.term.customer` | business_term | DataObject customers |

## 4. 六类结果（最终一轮，每类 3 次）

| 类 | 问题 | BusinessQuery（真实模型输出）| 状态 | 行数 | 数值 |
| --- | --- | --- | --- | --- | --- |
| A 单指标 | 总实收销售额是多少？| `metrics=[实收销售额]` | COMPLETED ×3 | 1 | **195660.0** |
| B 指标+维度 | 按下单地区看实收销售额 | `metrics=[实收销售额], dimensions=[下单地区]` | COMPLETED ×3 | 14 | 各地区求和 |
| C 过滤 | 下单地区为上海的实收销售额是多少？| `+ filters=[下单地区 = 上海]` | COMPLETED ×3 | 1 | **14008.0** |
| D 时间+排名 | 近30天按下单地区看实收销售额前10名 | `dimensions=[下单地区], time_expression=近30天, ranking=top 10` | COMPLETED ×3 | 10 | 见第 5 节 |
| E 歧义 | 所有订单的营业额合计是多少？| `metrics=[营业额]` | **CLARIFICATION_REQUIRED ×3** | — | — |
| F 关系 join | 按客户地区看实收销售额 | `metrics=[实收销售额], dimensions=[客户地区]` | COMPLETED ×3 | 14 | 各地区求和 |

B 额外追加 7 次复核：**7/7 完成**（另有 3 次未进入管线，因端点返回 429，既不计通过也不计失败）。

## 5. 逐类证据

### A · 单指标

```sql
SELECT SUM(t0."amount") AS "sum_amount" FROM "orders" AS t0 LIMIT ?
```
参数化：1 占位符 / 1 参数。结果 `{"sum_amount": 195660.0}`。

### B · 指标 + 维度

```sql
SELECT t0."delivery_region", SUM(t0."amount") AS "sum_amount" FROM "orders" AS t0
GROUP BY t0."delivery_region" ORDER BY t0."delivery_region" ASC LIMIT ?
```
参数化：1 占位符 / 1 参数。返回 14 行。

### C · 过滤（参数绑定）

```sql
SELECT SUM(t0."amount") AS "sum_amount" FROM "orders" AS t0
WHERE t0."delivery_region" = ? LIMIT ?
```
**参数元组 = `('上海', 200)`**——业务值在参数里，SQL 文本中只有占位符。结果 14008.0。

### D · 时间 + 排名（受治理时间轴）

```sql
SELECT t0."delivery_region", SUM(t0."amount") AS "sum_amount" FROM "orders" AS t0
WHERE t0."order_date" >= ? AND t0."order_date" <= ?
GROUP BY t0."delivery_region" ORDER BY "sum_amount" DESC LIMIT ?
```
参数化：3 占位符 / 3 参数；归一化时间范围 = `2026-08-22 .. 2026-09-20`。

关键点：模型给出的 `dimensions` 只有「下单地区」，**没有**「下单日期」；时间轴仍被确定性命中
（4/4 次绑定），因为轴来自受治理资产声明，而不是从已落地维度里嗅探数据类型。

Top-5 结果：北京 10206.0 / 深圳 9931.0 / 广州 9660.0 / 上海 9540.0 / 成都 9511.0。

### E · 歧义（正确阻断执行）

澄清载荷（三次完全一致）：

```json
[{"question": "“营业额”有多个企业定义，请确认你指的是：含运费营业额、营业额",
  "options": ["含运费营业额", "营业额"]}]
```

核验：`plan = None`（无 QueryPlan）、无 SQL、不访问业务库；用户只看到可读 label；
同一份载荷里**不含** `obj_` / `field_` / `edge_` / `graph_` / `ds_` 任何标识。

### F · 真实 Relationship Join

经图里**已确认的 `RELATES_TO`**（`orders.customer_id → customers.id`，`source=database`、
`confirmed=true`）连接，返回 14 行。Join 不是由同名字段、`*_id` 命名习惯或模型猜测产生的。

## 6. 独立校验（不依赖管线自证）

| 校验 | 结果 |
| --- | --- |
| A 的 195660.0 vs 纯 SQLite `SUM(amount)` | **一致** |
| C 的 14008.0 vs 纯 SQLite `SUM(amount) WHERE delivery_region='上海'` | **一致** |
| B 的 14 组求和 == A 的全量合计 | **成立** |
| C 的值 == B 结果里上海分项 | **成立** |
| D 的 Top-5 vs 纯 SQLite 近30天分组排名 | **逐名一致** |

## 7. 核验要点

- LLM 只产出业务意图；`entities` / `metrics` / `dimensions` / `ranking` 中没有任何表名、
  字段名、关系或 SQL。
- Grounding 只使用图确认的物理资产与受治理语义资产；未落地表达一律失败关闭。
- 原生查询只读、参数化，并通过 plan / revision 校验。
- Evidence 含 `plan_id` / `datasource_id` / `scan_version` / `display_command` /
  `row_count` / `truncated`，**不含参数值**。

## 8. 未保留的证据项（诚实说明）

下列字段在**当时**确实打印过并用于判定，但原始日志位于 `/tmp`，已被系统清理，
因此本报告无法逐字引用：

- 每题的 `plan_id` 与 `scan_version`（证据里有，但未落盘留存）；
- 每一轮的 `retrieval_candidates` 完整列表与 `grounding` 原始 JSON；
- 18 次运行的逐次日志。

**已据此加固**（见 `smartdata/scripts/acceptance/phase1_e2e.py`）：

1. 报告**默认写入** `SmartDataArtifacts/acceptance/phase1/phase1_e2e_report.json`，
   不再依赖调用方重定向 stdout。
2. 单题失败**不再中断整轮**：失败就地记录为该题的 `aborted`，其余题继续；
   且**每题结束后立即重写报告**，进程被打断也留有已观测部分。
3. 错误记录里带上端点返回体（`status_code` / `body`），使 `429` 这类失败能直接看出原因是
   限流、余额不足还是模型名错误。

## 9. 当前环境状态与复现方法

**当前状态：模型端点不可用。** 2026-09-20 14:05 CST 起，最小请求连续三次返回：

```text
HTTP 429  {"error":{"message":"insufficient balance","request_id":"..."}}
```

即账户额度已耗尽。这不是代码或管线问题——管线本身在额度可用期间通过了 18/18 验收。

额度恢复后复现完整验收（一条命令，六题、单进程、报告自动落盘）：

```bash
cd /Users/wingyouth_is01/code/SmartData
.venv/bin/python3 -m smartdata.scripts.acceptance.phase1_e2e
# 报告：SmartDataArtifacts/acceptance/phase1/phase1_e2e_report.json
```

前置条件：Docker 运行 + `smartdata-neo4j` 容器已启动（`bolt://localhost:7687`），
`.env` 里 `SMARTDATA_MODEL_*` 额度有效。

## 10. 记录来源

| 来源 | 内容 |
| --- | --- |
| Git history 中已删除的 `docs/evaluation-and-acceptance.md` §5.2 | 当时随代码提交的验收结果、环境、核验要点与已知限制；当前 docs 已收敛，历史仍可由 Git 追溯 |
| Git history 中已删除的 `.workbuddy/memory/2026-09-20.md` §17 / §24 | 当时逐类实测数值、SQL、参数与独立校验记录；工作状态文件现已停止跟踪 |
| git 提交 `9b96fc8` / `d4cd0b4` | 修复内容与验收脚本的提交说明 |
| 本文件 | 汇总；并对未保留项与当前环境状态作明确标注 |
