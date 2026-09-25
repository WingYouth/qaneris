# Excel Ingestion Engineering Specification(Excel 导入工程规范)

**Work Package：RS-EXCEL-01 Excel End-to-End**  
**Status：EXCEL-01A CORE DONE / EXCEL-01B PRODUCT CLI DONE / EXCEL-01C WEB UPLOAD + ROADSHOW E2E DONE / RS-EXCEL-01 DONE — Core Ingestion、Product CLI、HTTP Upload 边界与 Web Upload + Grounding → Query → Result 路演链路均已实现并通过真实 Neo4j + 真实模型网关 acceptance**  
**Authority：不得突破 `Qaneris_Next_Generation_Architecture_and_Implementation_Plan_v1.0.md`、`development-roadmap.md`、`product-interfaces.md` 的信任边界。**

## 1. Goal(目标)

Excel 是 Ingestion Source(导入源)，不是第二套 Query Engine(查询引擎)。

Roadshow 正式链路固定为：

```text
.xlsx Upload
→ Deterministic Validation
→ Managed Artifact
→ Deterministic Materialization
→ Immutable SQLite
→ Existing Secure Datasource / Initialization
→ Existing ScanSnapshot
→ Neo4j Enterprise Data Graph
→ Existing Grounding / Query / Evidence
→ Result
```

第一版只接受结构化、表格型 `.xlsx`。Excel 导入层只负责把文件稳定、可重复地转换成 SQLite；从 SQLite 开始全部复用 Qaneris 已有可信主链。

## 2. Trust Boundary(信任边界)

必须满足：

1. 不允许 LLM 自动重命名 Sheet、Column 或推断物理字段。
2. 不允许根据同名列、样本值重合或模型判断自动创建 Relationship。
3. 不创建 Excel 专用 Grounding、Planner、Query Generator 或 Execution 路径。
4. Materialized SQLite 必须通过现有 SQLite Adapter、DatabaseInitializer、Scan、Neo4j、Grounding 和 Query Engine。
5. 浏览器只负责上传和展示状态，不在前端解析企业语义。
6. 原始 Excel、Materialized SQLite 都按企业数据处理，不写进 Git、Prompt、普通日志或公开 Trace。

## 3. Scope / Non-goals(范围与非目标)

### 3.1 Roadshow v1 支持

- 文件格式：`.xlsx`。
- 多 Sheet Workbook。
- 每个可导入 Sheet 映射为一个 SQLite table。
- 基础值类型：null、boolean、integer、real、date、datetime、text。
- 确定性 Validation、Type Inference、SQLite Materialization。
- Managed Artifact、SHA-256、幂等导入。
- 复用现有 Scan → Neo4j → Ask 主链。
- CLI `qaneris source import-excel`。
- HTTP `POST /api/datasources/import-excel` multipart 上传与 Web Excel Upload 卡片。
- 后续 Web/API/MCP 只编排同一个 Application Service。

### 3.2 v1 明确不支持

- `.xls`、`.xlsm`、`.xlsb`、ODS、CSV。
- VBA / Macro。
- 执行 Excel Formula。
- Pivot Table、Chart、Named Range 作为数据源。
- 任意报表版式、跨区域表格、多个表放在同一 Sheet。
- 自动推断跨 Sheet Relationship。
- 自动把 Sheet 业务含义交给 LLM 判断。
- 跨数据源 Join。
- 在浏览器端生成 SQLite。

## 4. Proposed Application Contract(应用契约)

建议新增稳定 contract；具体 Pydantic 文件位置按现有 contracts 结构实现：

```text
ExcelImportRequest
- file_path / managed_upload_handle
- datasource_name
- workspace_id = "default"

ExcelImportResult
- import_id
- workspace_id
- datasource_id
- snapshot_id
- scan_version
- status
- original_filename
- file_sha256
- sqlite_path
- artifact_directory
- sheets[]
- warnings[]
```

Application Service 对外只暴露一个入口，例如：

```python
QanerisService.import_excel(...)
```

内部可以委托 `ExcelIngestionService`，但接口层不得直接调用 parser / materializer。

## 5. File Preflight(文件预检)

在 openpyxl 打开 Workbook 前执行预检。

默认 Roadshow 限制：

| Limit | Default |
| --- | ---: |
| File size | 25 MiB |
| Visible sheets | 32 |
| Rows per sheet including header | 100,000 |
| Columns per sheet | 256 |
| Total non-empty data cells | 1,000,000 |
| Text cell length | 32,768 chars |
| XLSX ZIP entries | 10,000 |
| Total uncompressed ZIP size | 200 MiB |

实现要求：

- 只接受 ZIP-based OOXML `.xlsx`，不能只信任文件后缀。
- 检查 ZIP entry 数和 uncompressed size，防止明显 zip bomb。
- 不把 XLSX 解压到公共临时目录。
- 不跟随 Workbook 中的 external link 发网络请求。
- `.xlsm` / VBA 文件直接拒绝，不尝试删除宏后继续导入。
- 上述默认值应集中在 `ExcelIngestionPolicy`，测试锁定默认值；以后可配置，但不能散落 magic number。

## 6. Workbook / Sheet Rules(工作簿与工作表规则)

### 6.1 Sheet inclusion

- 只导入 `sheet_state == visible` 的 Sheet。
- hidden / veryHidden Sheet 跳过并记录 warning。
- 完全空 Sheet 跳过并记录 warning。
- 至少必须有 1 个可导入 Sheet，否则导入失败。

### 6.2 Tabular shape

Roadshow v1 只接受标准表格：

- 第 1 行固定为 Header。
- 第 2 行开始是数据。
- Header 之前不允许标题、说明段或空行。
- fully-empty data rows 跳过。
- 某一数据行只要有任意非空 cell，就作为一条记录。
- 数据行出现 Header 范围之外的非空 cell，导入失败。
- merged cell 不支持；Workbook 只要可导入 Sheet 中存在 merged range，就返回明确错误。

不做“自动寻找 Header 行”，避免启发式规则影响可重复性。

## 7. Identifier Rules(Sheet/Table 与 Header/Column)

不使用 LLM 重命名。

### 7.1 Table name

`table_name` 由 Sheet name 确定：

1. Unicode NFKC normalize。
2. 去除首尾 whitespace。
3. 不转英文、不翻译、不 lower-case。
4. 原样作为 SQLite quoted identifier 使用。
5. 禁止 NUL character。
6. 规范化后不能为空。
7. Workbook 内按 `casefold()` 比较必须唯一。

Excel 本身 Sheet name 长度有限，因此 v1 不再额外截断。

### 7.2 Column name

Header cell：

1. 必须是 scalar value。
2. 转成字符串后 Unicode NFKC normalize。
3. trim 首尾 whitespace。
4. 不自动翻译、不生成 `column_1`。
5. 不能为空。
6. 长度最大 128 Unicode code points。
7. 同一 Sheet 内按 `casefold()` 必须唯一。
8. 禁止 NUL character。

重复/空 Header 必须让用户修复文件；不得静默 suffix，从而避免业务字段被悄悄改名。

Materialization 必须正确 quote SQLite identifier，不能通过字符串拼接形成可执行 SQL 注入。

## 8. Formula Policy(公式策略)

Roadshow v1：

> **不执行公式，也不使用缓存公式结果代替真实值。**

任何可导入 Sheet 出现 Formula cell，整个导入失败，返回：

```text
EXCEL_FORMULA_UNSUPPORTED
```

错误中允许返回 Sheet name + cell coordinate，但不得回显 Formula body。

理由：

- openpyxl 不是 Excel 计算引擎；
- cached value 可能过期；
- 相同业务文件在不同保存状态下会产生不可解释差异；
- 路演版本优先保证确定性。

后续若支持 Formula，必须单独定义 calculation provenance，不在 v1 偷偷降级。

## 9. Cell Value & Type Inference(单元格与类型推断)

### 9.1 Allowed input values

接受：

- `None`
- `bool`
- `int`
- `float`
- `str`
- `datetime.date`
- `datetime.datetime`

其他复杂 cell/value 类型返回明确 validation error。

### 9.2 Type lattice

每列扫描全部非空数据 cell，并按以下确定性规则得出最终类型：

```text
NULL       → 不参与类型决定
BOOLEAN    → BOOLEAN
INTEGER    → INTEGER
REAL       → REAL
DATE       → DATE
DATETIME   → DATETIME
TEXT       → TEXT
```

Widening：

- INTEGER + REAL → REAL
- DATE + DATETIME → DATETIME
- 任意类型 + TEXT → TEXT
- BOOLEAN 与 INTEGER/REAL 混合 → TEXT
- DATE/DATETIME 与 NUMBER/BOOLEAN 混合 → TEXT
- 不能落入以上确定规则的混合 → TEXT

空列最终声明为 TEXT，并在 manifest 中标记 `all_null=true`。

### 9.3 SQLite representation

| Inferred type | SQLite declared type | Stored value |
| --- | --- | --- |
| BOOLEAN | BOOLEAN | 0 / 1 |
| INTEGER | INTEGER | integer |
| REAL | REAL | float |
| DATE | DATE | ISO `YYYY-MM-DD` |
| DATETIME | DATETIME | ISO 8601 naive datetime |
| TEXT | TEXT | string |

- Excel time-only value暂不建立独立 TIME 类型，按 TEXT 保存。
- NaN / Infinity 不作为正常 REAL 导入；遇到时 validation fail。
- 不根据 Excel number format 猜业务语义，例如邮编/手机号不会被模型纠正；Excel 文件自身存储类型是事实来源。

## 10. Relationship Policy(关系策略)

Materialized SQLite：

- 不自动创建 Foreign Key。
- 不根据 Sheet / Header 名称推断 Relationship。
- 不根据 sample value 重合推断 Relationship。
- 不让 LLM 创建 Relationship。

因此多 Sheet Excel 第一版可能得到：

```text
relationships = 0
```

这是合法结果。

若未来支持用户显式声明关系，必须先校验两个 Table/Column 真实存在，再作为 configuration-confirmed relationship 进入现有 Scan/Graph contract。

## 11. Managed Artifact & Storage(受管产物)

统一复用：

```python
qaneris.common.artifacts.artifact_root()
```

Excel artifact 不得落在 Git repository 内。

建议目录：

```text
QanerisArtifacts/
└── ingestion/
    └── excel/
        └── <workspace_digest>/
            └── <policy_key>/
                └── <file_sha256>/
                    ├── source.xlsx
                    ├── data.sqlite3
                    └── manifest.json
```

其中：

```text
workspace_digest = sha256(workspace_id UTF-8)[:12]
policy_key       = <sanitized policy_version> + "-" + sha256(canonical policy JSON)[:12]
file_sha256      = SHA-256 of exact uploaded bytes
import_id        = "excel_" + workspace_digest + "_" + policy_digest + "_" + file_sha256[:16]
```

不要把原 filename 直接用于目录名。

`policy_key` 是 policy 的内容摘要，而不是单纯的版本字符串：除了 `policy_version`，任何 limit 变化都会得到新的 `policy_key`。这样做的目的是让 artifact identity 与 policy 严格对应——不同 policy 绝不落到同一个目录，因此也绝不会覆盖已经 finalized 的 `source.xlsx` / `data.sqlite3`。`<policy_key>` 中 sanitized version 只用于可读性，唯一性由摘要保证；该段不含路径分隔符，也不会是 `.` 或 `..`。

同一 `<workspace_digest>/<policy_key>/<file_sha256>` 目录内的 artifact 是 immutable：`source.xlsx` 与 `data.sqlite3` 一旦 finalize 就不再被覆盖（retry 复用已 finalize 的 SQLite，只在文件缺失或形状不符时才重新 materialize）。只有 `manifest.json` 会随生命周期状态更新。

### 11.1 Permissions

- Artifact root 到 import directory 之间的每一层（`ingestion/`、`excel/`、workspace、policy、file）：0700。
- `source.xlsx`：0600。
- materialization 完成后的 `data.sqlite3`：只允许服务账号读取；逻辑上 immutable。
- 临时文件使用同一私有目录内不可预测名称。
- 失败路径也必须清理未完成 SQLite/temp files。

### 11.2 Manifest

`manifest.json` 至少记录：

```text
format_version
policy_version
import_id
workspace_id
original_filename
file_sha256
file_size
created_at
status
sqlite_path
datasource_id
snapshot_id
scan_version
sheets:
  original_name
  table_name
  row_count
  column_count
  columns:
    source_header
    column_name
    inferred_type
    nullable
    all_null
warnings
error_code
```

Manifest 不记录完整业务 row/cell value。

`source.xlsx` 和 `data.sqlite3` 一旦 finalized 不再修改；manifest 允许更新生命周期状态。

## 12. Materialization Transaction(物化事务)

必须使用 staging → validation → atomic finalize：

```text
validate workbook
→ create private staging sqlite
→ BEGIN transaction
→ CREATE TABLE(s)
→ parameterized INSERT
→ COMMIT
→ PRAGMA integrity_check
→ close writer
→ atomic rename to data.sqlite3
→ open through existing SQLiteAdapter in read-only mode
```

要求：

- 所有 data INSERT 使用 parameter binding。
- Table / Column identifier 必须经过专用 quote helper。
- 任一 Sheet materialization 失败时整个 Workbook 失败，不留下“半个 Workbook”的正式 SQLite。
- 不对 finalized SQLite 执行 ALTER / UPDATE。
- Existing SQLiteAdapter 继续使用 `mode=ro` + `PRAGMA query_only=ON`。

## 13. Idempotency(幂等)

幂等键：

```text
workspace_id + exact file SHA-256 + policy_version
```

同一 workspace 重复上传相同 bytes：

- 已有 READY import：返回已有 `import_id / datasource_id / snapshot_id`，不重新写 SQLite、不重新创建 datasource。返回前会再次确认 Neo4j publication；确认不了就报错，不会返回一个无法自证的 READY，也不会改写已 finalized artifact 的 READY 记录。
- 已有 FAILED / PARTIAL import：允许从安全阶段 retry，但不能覆盖已经 finalized 的 source/sqlite。retry 会重新执行 datasource / scan / publication，而不是因为“datasource 和 snapshot 看起来齐全”就跳过发布。
- 文件 bytes 不同，即使文件名相同，也视为新 import。
- policy 不同（version 或任一 limit）：artifact identity 不同，是新的 import。

v1 不提供 `force_new` 绕过幂等。

## 14. Integration with Existing Qaneris Chain(接入现有主链)

Materialization READY 后构造现有安全连接：

```text
SecureDatasourceCreate
  kind = relational
  driver = sqlite
  authentication = none
  endpoint.path = <managed data.sqlite3>
```

然后只调用现有 Application / Initialization path：

```text
QanerisService.create_secure_datasource()
→ SQLiteAdapter.test_connection()
→ DatabaseInitializer.initialize()
→ scan_metadata / scan_relations / samples
→ ScanSnapshot
→ Neo4j publication
→ profile
→ datasource READY
```

不得直接写 Catalog 的 Dataset/Field/Relation 表。
不得直接写 Neo4j 绕过 `DatabaseInitializer`。

Excel import 成功的定义不是“SQLite 文件生成了”，而是：

```text
artifact READY
+ datasource READY
+ active ScanSnapshot READY
+ Neo4j publication successful
```

`status = READY` 与 `neo4j_publication_verified = true` 必须同时成立。没有真实 `GraphReader`（例如使用 `NullGraphReader`）或无法确认发布时，import 不得标记 READY；`READY` + `neo4j_publication_verified = false` 不是合法结果。

datasource / scan / Neo4j publication 任一阶段失败，都必须留下 `manifest.status = FAILED`，错误码取 `EXCEL_DATASOURCE_CREATION_FAILED` 或 `EXCEL_SCAN_FAILED`；不得伪造 `datasource_id` / `snapshot_id`，也不得回显业务 cell value。

Roadshow 最终 E2E 还必须继续通过现有 `QanerisService.ask()` 得到 Result。

## 15. Error Model(错误模型)

建议统一 `ExcelIngestionError`，至少有稳定 code：

```text
EXCEL_INVALID_EXTENSION
EXCEL_INVALID_CONTAINER
EXCEL_FILE_TOO_LARGE
EXCEL_ZIP_LIMIT_EXCEEDED
EXCEL_NO_IMPORTABLE_SHEET
EXCEL_SHEET_LIMIT_EXCEEDED
EXCEL_ROW_LIMIT_EXCEEDED
EXCEL_COLUMN_LIMIT_EXCEEDED
EXCEL_CELL_LIMIT_EXCEEDED
EXCEL_MERGED_CELL_UNSUPPORTED
EXCEL_FORMULA_UNSUPPORTED
EXCEL_INVALID_HEADER
EXCEL_DUPLICATE_HEADER
EXCEL_INVALID_CELL_VALUE
EXCEL_MATERIALIZATION_FAILED
EXCEL_INTEGRITY_CHECK_FAILED
EXCEL_DATASOURCE_CREATION_FAILED
EXCEL_SCAN_FAILED
```

错误可以返回 Sheet/row/column coordinate；不得返回敏感 cell value。

## 16. Proposed Code Boundary(建议代码边界)

```text
qaneris/
├── ingestion/
│   └── excel/
│       ├── __init__.py
│       ├── contracts.py
│       ├── policy.py
│       ├── validator.py
│       ├── types.py
│       ├── materializer.py
│       └── service.py
│
├── application/
│   └── service.py          # 只增加 import_excel 编排入口
│
├── interfaces/
│   └── api/
│       ├── app.py          # /api/datasources/import-excel 路由
│       └── excel.py        # staging / 早期大小保护 / 产品安全投影 / Excel 错误映射
│
└── cli/
    ├── main.py             # dispatch / stdout-stderr 隔离 / exit code / 安全 error boundary
    └── source.py           # source test / scan / import-excel 命令注册与输出格式化
```

职责：

- `validator.py`：文件、Workbook、Sheet、Header、Formula、limits。
- `types.py`：纯确定性的 type inference / widening。
- `materializer.py`：只负责写 staging SQLite 与 integrity check。
- `service.py`：managed artifact + idempotency + 调用现有 QanerisService datasource/scan 路径。
- `interfaces/api/excel.py`：只做 HTTP 边界三件事——临时 staging、读取时的大小保护、产品安全投影；不复制任何 ingestion 规则。
- Interface 层不得复制以上规则。

### 16.1 Product CLI Interface（EXCEL-01B）

正式命令：

```text
qaneris source import-excel FILE [--name NAME] [--workspace WORKSPACE_ID] [--json]
```

| Parameter | 含义 |
| --- | --- |
| `FILE` | 必填 positional `.xlsx` 路径 |
| `--name` | 可选 `datasource_name`；缺省由 Core 生成 `Excel: <filename>` |
| `--workspace` | 可选 workspace id，默认 `default` |
| `--json` | 输出纯机器可解析 JSON |
| `--env-file` | 复用既有全局选项，不新增 dotenv 机制 |

CLI 只负责 `argument registration → ExcelImportRequest → QanerisService.import_excel()` 与输出格式化。禁止新增 `--force`、`--skip-validation`、`--no-scan`、`--no-neo4j`、`--unsafe` 这类会破坏本规范确定性契约的开关。CLI 也不得直接调用 validator / materializer / `ExcelIngestionService` 内部 / Catalog dataset API / `DatabaseInitializer` / Neo4j writer / SQLite writer。服务实例按正常 runtime 配置构造（`QANERIS_CATALOG` 与 API、MCP、`doctor` 同约定），不注入 fake graph 或 `NullGraphReader`。

Human output（成功）至少包含 `[PASS] Excel import ready`、`Datasource`、`Snapshot`、`Scan version`、`Sheets`、`Artifact`（受管 artifact directory，而不是内部 `sqlite_path`）；可选 `Name`、`Import ID`、逐条 `Warnings`。不得打印业务 cell value、原始 row、完整 workbook、连接 secret 或环境变量值。

`--json` 输出单一合法 JSON document（`ExcelImportResult` 的稳定字段投影），成功形如：

```json
{
  "status": "READY",
  "import_id": "...",
  "workspace_id": "default",
  "datasource_id": "...",
  "snapshot_id": "...",
  "scan_version": 1,
  "original_filename": "orders.xlsx",
  "file_sha256": "...",
  "artifact_directory": "...",
  "policy_version": "excel-ingestion-v1",
  "neo4j_publication_verified": true,
  "sheets": [],
  "warnings": []
}
```

`stdout` 只允许 JSON；driver / library 的 incidental stdout 通过 `redirect_stdout(sys.stderr)` 隔离。

错误输出必须保留稳定 Excel error code（不得退化成通用 `operation_failed`），失败形态：

```json
{
  "status": "operation_failed",
  "error": {
    "type": "ExcelIngestionError",
    "code": "EXCEL_FORMULA_UNSUPPORTED",
    "message": "...",
    "sheet": "orders",
    "coordinate": "B3"
  }
}
```

可以返回 sheet / cell coordinate，但不得回显 formula body、业务 cell value 或 secret。

Exit codes 沿用现有 CLI 统一约定：

```text
0 = import READY（含已验证的 Neo4j publication）
1 = import 被拒绝 / scan 或 publication 失败 / 环境阻塞
2 = CLI 用法或配置错误（argparse 错误、无效 --name/--workspace、无法加载 env file）
```

幂等由 Application Service 负责；CLI 不提供 `force refresh`、`rescan` 或新建 datasource 的捷径，重复执行同一文件只展示既有结果。

CLI 验收脚本：`qaneris/scripts/acceptance/import_excel_cli.py`（以真实 console script 运行真实 Neo4j）。

### 16.2 HTTP Upload Interface（EXCEL-01C）

正式端点：

```text
POST /api/datasources/import-excel
Content-Type: multipart/form-data
```

| Form field | 必填 | 含义 |
| --- | --- | --- |
| `file` | 是 | `.xlsx` 字节流；浏览器只能提交字节，不提交任何服务器路径 |
| `name` | 否 | datasource 名称，trim 后 1..100 字符；缺省由 Core 生成 |
| `workspace_id` | 否 | workspace id，默认 `default`，trim 后不得为空 |

禁止存在 `file_path`、`force_new`、`skip_validation`、`no_scan`、`no_neo4j` 等开关；未知 form 字段被忽略且不产生任何效果，端点只把 `file` / `name` / `workspace_id` 组装成 `ExcelImportRequest` 交给 `QanerisService.import_excel()`，不得直连 validator、materializer、Catalog dataset API、`DatabaseInitializer` 或 Neo4j writer。

端点本身是 `async`（读取 multipart 必须 `await`），但 `import_excel()` 是阻塞调用。它必须在线程池中执行（`run_in_threadpool`），不得直接跑在事件循环上：否则一次慢导入会让整个服务失去响应，包括 `/health`。

成功返回 HTTP 201，且只包含产品安全字段（`ExcelImportResult` 白名单投影）：

```json
{
  "status": "READY",
  "import_id": "...",
  "workspace_id": "default",
  "datasource_id": "...",
  "snapshot_id": "...",
  "scan_version": 1,
  "original_filename": "orders.xlsx",
  "file_sha256": "...",
  "policy_version": "excel-ingestion-v1",
  "neo4j_publication_verified": true,
  "sheets": [],
  "warnings": []
}
```

与 CLI `--json` 的关键差异：HTTP 投影**不含** `artifact_directory`，也不含 `sqlite_path` / `manifest_path` / 临时上传路径。浏览器不需要服务器侧位置，暴露它只会扩大泄漏面。

错误返回：

| 状态 | 触发 | body |
| --- | --- | --- |
| 201 | import READY 且 Neo4j publication 已验证 | 上述成功投影 |
| 400 | 其他稳定 Excel 错误码（含 `EXCEL_FORMULA_UNSUPPORTED`、`EXCEL_INVALID_EXTENSION`、`EXCEL_NOT_A_CONTAINER`、`EXCEL_SCAN_FAILED` 等） | `{"error": {"code", "message", "sheet", "coordinate"}}` |
| 413 | `EXCEL_FILE_TOO_LARGE` | 同上 |
| 422 | 请求层问题（缺 `file` part、`filename` 为空等 FastAPI 校验失败） | `{"error": {"code": "validation_error", ...}}` |
| 500 | 非预期异常 | 通用错误，message 已脱敏，不回显内部路径或 cell value |

必须保留稳定 Excel error code，不得被通用 `QanerisError` handler 压成 `excel_ingestion_failed` 或 `invalid_request`；`code` 是调用方唯一可分支的契约。`sheet` / `coordinate` 允许出现在 message 与结构化字段中，但绝不回显 formula body、业务 cell value 或 secret。

临时文件生命周期（`qaneris/interfaces/api/excel.py`）：

- 私有大写目录：`<artifact_root>/uploads/`，mode `0700`（artifact root 必须在 repository 之外）。
- 每次上传用 `mkdtemp(prefix="excel-upload-")` 生成不可预测子目录，文件以 `O_CREAT|O_EXCL|O_WRONLY` 创建，mode `0600`。
- 只保留客户端文件名的最后一段（`basename`）；含 NUL/控制字符、空名、`.`/`..` 直接拒绝为 `EXCEL_INVALID_EXTENSION`；`../` 无法逃出私有目录。文件名超长按尾部截断。
- 大小保护在**读取过程中**生效，上限取 `ExcelIngestionPolicy.max_file_size_bytes`（不复制第二份常量）：超限立刻 `EXCEL_FILE_TOO_LARGE` → 413，不把整个 body 读进内存。
- 无论成功、业务拒绝还是异常，`finally` 都删除整个 staging 目录。

### 16.3 Web Excel Upload + Minimal Ask Slice（EXCEL-01C）

前端复用既有 `web/frontend/`（Vite + React Foundation），不新建第二套前端：

```text
src/api/excel.js        importExcel() / listDatasources()      （multipart 契约）
src/api/upload.js       uploadFormData()（XHR，真实 byte progress）/ createFetchTransport()
src/api/ask.js          askQuestion()                          （POST /api/ask）
src/excel/importState.js   上传状态机 + 错误/结果投影
src/excel/datasourceScope.js 导入后自动选择 datasource scope
src/ask/askView.js      四态 Ask 视图（completed / clarification / failed / error）
src/components/excel/ExcelImportCard.jsx
src/components/datasource/DatasourceScope.jsx
src/components/ask/{AskPanel,ResultTable,ClarificationPanel,EvidencePanel}.jsx
src/App.jsx             编排：上传 → 刷新 datasource 列表 → 自动选中刚导入的 datasource_id 作为 Ask Scope → 提问 → 结果/证据
```

规则：

- FormData 只包含 `file` / `name` / `workspace_id`，不提交任何服务器路径或开关。
- 上传成功后必须刷新 datasource 列表，并把**导入返回的** `datasource_id` 设为 Ask Scope，而不是复用上一次的选择。
- 浏览器只做上传与展示，不解析企业语义、不生成 SQL；图表/可视化不在本切片范围。
- 请求失败（非 2xx）才重试；`clarification_required` / `completed` / `failed` 都是产品结果，不得重试掩盖。
- 前端不得伪造上传进度：fetch transport 拿不到 byte progress 时不显示百分比。`accept=".xlsx"` 只是 UX，真实校验始终在服务端。

业务语义边界：Excel 导入发布的是**物理结构**。问题要绑定 metric / dimension 槽位，必须由管理员通过既有 `SemanticAssetBootstrap` 显式注册受治理资产（本切片不做任何自动 FK 或关系推断）。这与其它数据源完全一致。

## 17. Delivery Slices(开发切片)

为避免一次改太多，RS-EXCEL-01 分三步：

### EXCEL-01A — Core Ingestion

完成：

```text
.xlsx file
→ Validation
→ Managed Artifact
→ Immutable SQLite
→ SecureDatasourceCreate
→ ScanSnapshot
→ Neo4j
```

只做 service-level API + unit/integration tests。

### EXCEL-01B — Product CLI

新增：

```text
qaneris source import-excel <file.xlsx>
  --name <datasource-name>
  --workspace <workspace>
  --json
```

CLI 只能调用 Application Service。

已实现：命令注册、human/JSON 输出、exit code 与稳定错误码见 §16.1；定向 CLI 验收见 `qaneris/scripts/acceptance/import_excel_cli.py`。

### EXCEL-01C — Roadshow E2E / Web

完成：

- HTTP upload route 与 Web Excel Upload UI。
- Upload progress / validation error 展示。
- 导入成功后自动成为可选择 Datasource。
- 固定 Excel acceptance workbook。
- 使用现有 `QanerisService.ask()` 完成至少一条真实 Grounding → Query → Result。
- 接入统一 Streaming contract 时只消费公开事件。

已实现：HTTP 边界见 §16.2，Web 切片见 §16.3。统一 Streaming contract 仍未实现，因此本切片只消费同步 `POST /api/ask`，不引入第二套进度协议。

验收脚本：`qaneris/scripts/acceptance/excel_web_ask.py`（真实 Neo4j + 真实模型网关 + 真实 HTTP multipart + 通过 Node 运行真实前端模块）。

## 18. Tests(测试要求)

### 18.1 Unit

至少覆盖：

- 非 xlsx。
- corrupt zip。
- zip limits。
- hidden/empty sheets。
- header 不在第一行。
- empty header。
- duplicate header。
- merged cell。
- formula cell。
- file/sheet/row/column/cell limits。
- type inference 每种单类型。
- INTEGER + REAL widening。
- TEXT widening。
- BOOLEAN + NUMBER → TEXT。
- DATE + DATETIME → DATETIME。
- all-null column。
- identifier quoting。
- parameterized insert。
- manifest 不含业务 row value。
- same hash idempotency。
- failed staging cleanup。

HTTP 上传边界另有 `tests/unit/interfaces/test_api_excel_upload.py`，覆盖：成功投影只含白名单字段、响应不出现任何内部路径、`import_excel()` 只被调用一次且参数透传、临时文件在调用期间存在且 mode `0600`、请求结束后 staging 目录为空、staging 根目录 mode `0700`、绝对 `../` 文件名不逃逸、formula / extension / container 拒绝保留稳定码、文件大小超限返回 413 且使用 policy 限额、缺 `file` part 返回 422、`name` / `workspace_id` 非法在进入 Core 前被拒、发布不可验证返回 `EXCEL_SCAN_FAILED`、非预期异常返回 500 且 message 已脱敏，以及**阻塞式 Core import 不占用事件循环**（并发 `/health` 必须在 1 秒内返回）。

### 18.2 Integration

至少一个真实 `.xlsx`：

```text
Upload
→ data.sqlite3
→ SQLiteAdapter read-only connection
→ SecureDatasourceCreate
→ DatabaseInitializer
→ ScanSnapshot READY
→ Neo4j validation
```

断言：

- Sheet count / table count 一致。
- Header / Field 一致。
- inferred types 一致。
- row count 一致。
- relationships = 0，除非未来显式配置。
- artifacts 在 repository 外。
- datasource 使用 managed SQLite path。
- 重复上传不重复生成 datasource/snapshot。

### 18.3 Roadshow E2E

固定一个不含公式、merged cells 的 workbook，例如单 Sheet `orders`：

```text
order_id
order_date
region
amount
status
```

完成：

```text
.xlsx
→ import
→ scan
→ graph
→ ask "按地区统计销售额"
→ grounding
→ validated SQL
→ read-only execution
→ typed result + evidence
```

需要记录 import_id、datasource_id、snapshot_id、scan_version、SQL display、row_count 与 expected result。

实现为 `qaneris/scripts/acceptance/excel_web_ask.py`，以真实 API 子进程 + 真实 Neo4j + 真实模型网关运行，覆盖 41 项断言：

```text
multipart upload  -> POST /api/datasources/import-excel   (201 READY, neo4j_publication_verified)
datasource read   -> GET  /api/datasources                (该 datasource 为 ready)
ask               -> POST /api/ask                        (completed, 无 clarification)
```

固定断言结果（fixture：单 Sheet `orders`，order_id / order_date / region / amount / status，5 行，无公式、无合并单元格、relationships = 0）：

```text
question        : 对 orders 表，按 region 分组汇总 amount 的总和。
generated sql   : SELECT t0."region", SUM(t0."amount") AS "sum_amount" FROM "orders" AS t0
                  GROUP BY t0."region" ORDER BY t0."region" ASC LIMIT ?
expected rows   : {'East': 350.5, 'West': 220.25}
actual rows     : {'East': 350.5, 'West': 220.25}
```

除正向链路外，同一脚本还验证：未受治理的 metric 不会静默给出数字；同字节重复上传幂等（同 import / datasource / snapshot，artifact 未被改动，不产生第二个 datasource）；含公式 workbook 在同一 HTTP 边界被拒（400 + `EXCEL_FORMULA_UNSUPPORTED` + sheet/coordinate，且不创建 datasource、不回显 formula body）；以及通过 Node 运行真实前端模块（`web/frontend/src/api/*`、`src/excel/*`、`src/ask/*`）验证浏览器请求契约与结果投影。

## 19. Definition of Done(RS-EXCEL-01 完成条件)

只有以下全部成立才可以把 Roadmap 的 Excel Ingestion 从 TODO 改为 DONE：

1. `.xlsx` 可以通过统一 Application Service 导入。
2. Validation / limits / formula / header / type rules 全部有自动化测试。
3. 原文件和 SQLite 被受管保存到 repository 外。
4. SQLite materialization 是 atomic、read-only after finalize、重复导入幂等。
5. Datasource 通过现有 Secure Datasource + DatabaseInitializer 路径创建。
6. ScanSnapshot READY，Neo4j publication / validation 成功。
7. 不创建 Excel 专用 Query Engine，不推断虚假 Relationship。
8. CLI `source import-excel` 可验收。
9. Roadshow 固定 Excel 能走到现有 Grounding → Query → Result。
10. 日志、Trace、manifest 不泄露完整业务 cell values 或敏感路径之外的企业数据。

全部条件已满足（1–7 由 EXCEL-01A、8 由 EXCEL-01B、9–10 由 EXCEL-01C 覆盖）。完成状态只在 `development-roadmap.md` 更新；本文只维护工程契约。
