# Qaneris Product Interfaces(产品接口设计)

## 1. Product Principle(产品原则)

Web(网页端)、API(接口)、CLI(命令行)、Skill(技能)和 MCP(模型上下文协议)必须共享 Qaneris Core(核心能力)与 Application Service(应用服务)。任何入口都不得复制 Intent(意图)、Retrieval(检索)、Grounding(物理绑定)、Planning(规划)、Validation(校验)或 Execution(执行)逻辑。

```text
Web / API / CLI / Skill / MCP
              ↓
Qaneris Application Contract
              ↓
QanerisService
              ↓
Intent → Retrieval → Grounding → QueryPlan → Validation → Execution
```

## 2. Current Source-backed State(当前源码状态)

| Surface | Current implementation | Roadshow gap |
| --- | --- | --- |
| API | Ask 同步/POST SSE、Excel 上传、Secure Datasource 生命周期、Adapter、TLS capability 与 Credential/Certificate HTTP 接口 | legacy `POST /api/datasources` 保留兼容；新 Web 仅使用 `/api/datasources/secure` |
| CLI | `doctor`、`acceptance phase1/ask`、`qaneris ask`（human/JSON/stream）、`source list/show/scan-one`、`source test/scan`、`source import-excel`、RS-CLI-02 的 `credential add/show/delete`、`certificate add-ca/add-client/inspect/delete`、`source test-one/create/update/delete` 已实现 | 最终 Secure CLI DONE（见 §7）；`source create` 仍不自动扫描，故创建后需显式 `scan-one` |
| MCP | RS-MCP-01 DONE。stdio server 暴露 `list_adapters` / `get_schema_context` / `summarize_schema` / `search_dataset` / `list_relations` / `list_mappings` / `list_governance_suggestions` / `list_datasources` / `inspect_datasource` / `test_secure_datasource` / `create_secure_datasource` / `update_secure_datasource` / `delete_datasource` / `scan_datasource` / `register_sqlite` / `ask_data`。Tool 只调用 `QanerisService`；Ask 只消费一次统一 `ask_stream()`，每个事件对应一个标准 MCP progress。MCP 只接收 `SecretReference` | Skill 已按该契约刷新；Web 端也消费统一 AskEvent |
| Skill | RS-SKILL-01 DONE。Skill 只调用现行 MCP Tool，Secure Datasource 固定 Test → Create → Scan，Update 为 Test → Update → Scan；只传 `SecretReference`；回答只依据 `answer` / `result` / `evidence`；Progress 是执行阶段而非思维链 | `tests/unit/skill/test_skill_contract.py` 为静态契约守卫 |
| Web | RS-WEB-01 + RS-VIZ-01 DONE：Vite + React 19 工作台、中文导航、POST SSE Ask/Trace、Secure Datasource Wizard/Manager、凭据证书上传、Excel 上传、Result/Evidence、受控 SVG 图表、production React dist 服务 | 生产部署无应用登录，须置于受控网络/VPN/反向代理访问控制后 |

### 2.1 Roadshow Product Foundation Dependency(路演产品基础依赖)

正式剩余路线统一为：

```text
Managed Credential Core (DONE)
→ Secure Datasource Lifecycle (DONE)
→ TLS Materializer / Driver TLS Matrix (DONE)
→ Credential / Certificate Product Boundary (DONE)
→ Secure CLI (DONE)
→ Excel Product-path Regression (DONE)
→ MCP (DONE)
→ Skill (DONE)
→ Web (NEXT)
→ Visualization
```

Roadshow 明确不开发 User Login / Session / JWT / OAuth / RBAC。没有应用层登录时，Web/API 只能部署在受控网络、VPN 或反向代理访问控制之后，不能直接作为开放公网服务。

**当前产品开发不等待 16 个服务数据库的本地 TLS/Scan 验收。** `RS-DB-01 / RS-TLS-01 / RS-DB-02` 延后到数据库正式部署到服务器后统一执行；它们仍是最终 Release Gate，但不阻塞 CRED-01B / CLI / MCP / Skill / Web 开发。

当前 `AskResponse` 已包含 `status`、`business_query`、`clarification`、`plan`、`result`、`evidence`、`error`、`analysis` 等结构；`ExecutionEvidence.display_command` 提供安全查询展示基础。它们是后续 Web/CLI/MCP 展示的现成后端事实，不等于前端功能已经完成。

## 3. Roadshow Unified Ask Experience(路演统一问数体验)

四种主要产品形态必须呈现同一条问数主链：

```text
Question
→ Intent
→ Semantic Retrieval
→ Grounding
→ Clarification OR QueryPlan
→ Native Query
→ Read-only Execution
→ Typed Result
→ Evidence
→ Visualization
```

产品展示的是 **Execution Trace(结构化执行轨迹)**，不是模型私有 Chain-of-Thought(思维链)。可以展示：当前阶段、已确认的语义资产、物理对象/字段摘要、QueryPlan、SQL/Native Query 的安全展示、执行状态、row_count、Evidence、错误和完成原因。不要展示模型隐藏推理文本。

## 4. Ask Event / Streaming Contract(问数事件与流式契约)

统一事件契约已经实现于 `qaneris/contracts/ask_events.py`，由 `QanerisService.ask_stream()` 产出，`ask()` 与 `ask_stream()` 共享同一套内部编排（`_ask_event_records`），同步行为保持不变：

```text
accepted
intent_ready
retrieval_ready
grounding_ready
clarification_required
plan_ready
query_ready
execution_started
result_ready
error
done
```

事件字段为 `event_type` / `sequence` / `payload` / `occurred_at` / `correlation_id`。`payload` 是每个事件类型的小型公开投影，刻意不是内部领域模型；契约在 `AskEvent` 的字段校验器里做最后一道防线：递归丢弃模型私有推理字段（`reasoning` / `chain_of_thought` / `thoughts`），脱敏凭据形态的键与值（password / token / api_key / private_key / certificate / connection_string、`Bearer ...`、`sk-...`、PEM 文本、URL 内嵌凭据），并遮蔽绑定参数值。

Transport(传输)：
- CLI 已消费该契约（RS-CLI-01B）：`qaneris ask --stream` 输出 human 轨迹，`qaneris ask --stream --json` 输出严格 JSONL，每行就是契约自身事件的序列化结果，CLI 不私设第二套 event schema。
- API 已消费该契约（RS-STREAM-01B）：`POST /api/ask/stream` 把同一个 `ask_stream()` 事件流按 SSE 输出（见下方 §4.1）。Web 通过 POST + fetch/ReadableStream 解析并校验事件，且只展示白名单公开摘要。
- MCP 已消费该契约（RS-MCP-01）：`ask_data` 把同一个 `ask_stream()` 事件流 1:1 映射为标准 MCP progress notification（`progress=event.sequence`、`message=event.event_type.value`、不传 `total`），终态响应取自事件 payload，不复制事件 schema。
- Skill 不创建独立传输层，而是消费 MCP 的进度与最终结果。

### 4.1 API SSE Transport(API 服务器发送事件传输，RS-STREAM-01B)

```http
POST /api/ask/stream
Content-Type: application/json

{"question": "...", "workspace_id": "default", "datasource_id": "ds_xxx", "max_rows": 200}
```

- Request Body(请求体) 与 `POST /api/ask` 完全相同，继续使用同一个 `AskRequest`；没有 `StreamAskRequest` 之类的重复请求模型。
- Response(响应)：`200` + `Content-Type: text/event-stream` + `Cache-Control: no-cache`，chunked。
- 每一帧：`event: <AskEvent.event_type>` + 换行 + `data: <AskEvent.model_dump_json()>` + 空行结束。`data` 直接来自契约自身的序列化结果，API 不重排、不重编号、不重新包装 `payload`。
- 客户端可通过 `AskEvent.model_validate_json(data)` 重新校验每一帧，这就是“API 没有第二套 schema”的证明。
- HTTP Status(状态码) 规则：流一旦建立就是 `200`。**clarification_required 与 error 都不转成 4xx/5xx**——`event_type` 是唯一结论来源。只有 `AskRequest` 自身校验失败（空 question、非法 `max_rows`、非法 JSON）才在流建立前由 FastAPI 返回 `422 validation_error`。
- 客户端断开时传输停止推进事件流，不继续写入已关闭连接；如果 `ask_stream()` 在契约之外异常中断，传输终止响应且**不伪造 `error` / `done`**，只在服务端记录一行不含消息内容的告警（仅异常类型）。
- API 不复制 redaction(脱敏) 规则：安全边界就是 `AskEvent` 本身，传输层不得绕开，也不得另建一套。

实测（真实 uvicorn + 真实模型网关 + `QANERIS_GRAPH_STORE=null`）：

```text
HTTP/1.1 200 OK
content-type: text/event-stream
cache-control: no-cache
transfer-encoding: chunked

event: accepted
data: {"event_type":"accepted","sequence":1,...}

event: intent_ready
data: {"event_type":"intent_ready","sequence":2,...}

event: error
data: {"event_type":"error","sequence":3,...}

event: done
data: {"event_type":"done","sequence":4,...}
```

无论哪个入口，产品展示的都是 **Execution Trace(结构化执行轨迹)**，不是模型私有 Chain-of-Thought(思维链)。

### 4.2 Credential / Certificate API(凭据与证书接口，RS-CRED-01B DONE)

固定 5 个产品入口，全部由 `qaneris/interfaces/api/credentials.py` 提供，全部只调用 `QanerisService` 的公开凭据方法（`create_managed_secret` / `create_managed_client_identity` / `inspect_managed_secret` / `delete_managed_secret`），不直接触碰 Store / Validator / Catalog / Resolver：

```text
POST   /api/credentials                      JSON 文本凭据（password / token / api_key / client_private_key_password）
POST   /api/certificates/ca                  multipart 文件字段 file（PEM 或 DER）
POST   /api/certificates/client-identity     multipart certificate + private_key + 可选 private_key_password
GET    /api/credentials/{secret_id}          任意 kind 的公开记录
DELETE /api/credentials/{secret_id}          任意 kind；仍被 datasource 引用则 409 managed_secret_in_use
```

- 响应只含产品字段 `secret_id` / `kind` / `created_at` / `metadata`（`ManagedCredentialPublic`），**不返回** value / plaintext / password / token / private key / 证书正文 / 文件名 / 服务器路径。client identity 返回 `{client_certificate, client_private_key}` 两个 **不同** 的 `secret_id`。
- JSON 入口只接受 4 个文本 kind；`ca_certificate` / `client_certificate` / `client_private_key` 必须走对应文件入口，防止 secret 与 certificate 混淆。
- `private_key_password` 是 **transient** 值：只用于解密上传的加密私钥，成功导入后不再需要，因此 **不创建** `CLIENT_PRIVATE_KEY_PASSWORD` secret。
- 单文件上限 1 MiB（`CREDENTIAL_FILE_MAX_BYTES`），64 KiB 分块有界读取；超限 413 `credential_upload_too_large`。证书/私钥只在内存 buffer 中处理，**不落临时磁盘**。
- client certificate + private key 在 **存储前** 完成 pair 校验；不匹配返回 `certificate_key_mismatch`，且不留任何已写入文件。两次写入按 all-or-nothing 处理，失败回滚。
- 本卡 **不提供** `GET /api/credentials` 列表接口（Store 暂无产品级 enumeration contract）；不新增 CLI / MCP upload / Web UI / Login。
- 未配置 `QANERIS_SECRET_STORE_DIR` / `QANERIS_MASTER_KEY` 时返回既有 `credential_store_configuration_error`，不自动生成 key、不回退明文。

## 5. Web Roadshow Workspace(Web路演工作台)

Roadshow Web 至少包含：

- Ask 输入、Workspace / Datasource Scope。
RS-WEB-01 已完成智能问数、Clarification、Result Table、Execution Trace、QueryPlan / `evidence.display_command`、Datasource / Scan 管理、Secure Datasource lifecycle、Excel Upload 与凭据/证书上传。Roadshow 无 Login/Auth，部署必须经过受控网络、VPN 或反向代理访问控制。

RS-VIZ-01 已实现：`Typed Result → deterministic Chart Policy → controlled ChartSpec → React SVG`。图表只消费现有 Ask View 的 table、plan、evidence；自动选择折线图/柱状图/表格，饼图需用户选择且数据满足约束。表格和 Evidence 始终可查看；不执行任意 JS/HTML，也不执行模型生成的可执行可视化配置。

当前 Web workspace 在 `web/frontend/`，由 React 19/Vite 构建；生产 FastAPI 优先服务 `web/frontend/dist/index.html` 与 `/assets/*`。正式一级导航为“智能问数 / 数据源 / Excel 导入”，默认进入智能问数。Analyze 主路径是 Conversation → Run → `GET /api/runs/{run_id}/stream?after_sequence=N` → 每轮自己的结果、图表与依据。Conversation 的数据源范围创建后固定；断线仅续订同一个 Run；刷新从 URL 中的 Conversation ID 恢复消息及最近 Run。历史结果按需读取，澄清、取消、重试均操作同一 Run。Run Timeline 只显示公开事件白名单，不显示私有推理或原始 payload；查询文本只显示安全的 `evidence.display_command`。`POST /api/ask` 和 `POST /api/ask/stream` 保留给旧客户端、CLI、MCP 与 Skill。安全数据源仅调用 `/api/datasources/secure`、test/update/scan/inspect/delete 生命周期，旧 `/api/datasources` 创建入口只保留兼容。TLS 控件消费 `GET /api/tls-capabilities`，秘密上传后只保留 SecretReference，绝不持久化在浏览器存储。

构建和回归命令：`cd web/frontend && npm ci && npm test && npm run build`。Docker 使用 Node 22 multi-stage 构建 React dist，再复制到 Python runtime，不携带 node_modules。

现有 `web/frontend/` React Foundation 保留并继续扩展；旧静态 `web/index.html + app.js` 只视为兼容资产，不作为 Roadshow 正式产品架构。

## 6. Excel Ingestion Product Contract(Excel导入产品契约，Roadshow Target)

**Current state：EXCEL-01A Core Ingestion、EXCEL-01B Product CLI、EXCEL-01C Web Upload + Grounding → Query → Result Roadshow E2E 均已完成；RS-WEB-01 Unified Ask Workspace 已实现。**

当前主仓库已经提供 `QanerisService.import_excel()`，完成确定性 `.xlsx` 校验、Managed Artifact、policy-scoped immutable SQLite、幂等、SecureDatasourceCreate → DatabaseInitializer → ScanSnapshot → Neo4j publication；真实 Neo4j acceptance 49/49 PASS。EXCEL-01B 已经把同一入口暴露为产品命令 `qaneris source import-excel FILE [--name] [--workspace] [--json]`，CLI 只编排 Application Service，不复制 ingestion 规则。EXCEL-01C 增加了 `POST /api/datasources/import-excel`（浏览器 multipart，只提交字节，返回产品安全字段投影，不暴露 `sqlite_path` / `artifact_directory` / 临时路径）与 Web Excel Import Card + 最小 Ask 切片，并用真实 HTTP + 真实 Neo4j + 真实模型网关验收。Excel 仍然是 Ingestion Source(导入源)，不是第二套查询引擎：

```text
.xlsx Upload
→ Deterministic Validation
→ Managed Artifact
→ Deterministic Materialization
→ Immutable SQLite
→ Existing Scan
→ Neo4j
→ Existing Grounding / Query / Evidence
```

首版不得用 LLM 自动重命名字段或根据同名列猜关系。Sheet → table、header/type 规则、公式处理、文件/行/单元格大小限制必须确定性。浏览器只负责上传和展示状态，不在前端解析企业语义。

工程契约（HTTP 端点字段、错误码与状态码映射、临时文件生命周期、Web 模块边界）见 `excel-ingestion.md` §16.2 / §16.3。

## 7. CLI Contract(CLI契约)

当前命令树（RS-CLI-01A + RS-CLI-01B + EXCEL-01B + RS-CLI-02 已完成）：

```text
qaneris doctor [--json]

qaneris credential add --kind {password|token|api_key|client_private_key_password} [--stdin] [--json]
qaneris credential show SECRET_ID [--json]
qaneris credential delete SECRET_ID [--json]

qaneris certificate add-ca FILE [--json]
qaneris certificate add-client --certificate FILE --private-key FILE
                              [--private-key-password-prompt | --private-key-password-stdin] [--json]
qaneris certificate inspect SECRET_ID [--json]
qaneris certificate delete SECRET_ID [--json]

qaneris source list [--workspace WORKSPACE_ID] [--json]
qaneris source show DATASOURCE_ID [--json]
qaneris source scan-one DATASOURCE_ID [--json]
qaneris source test-one --config CONFIG [--json]
qaneris source create --config CONFIG [--json]
qaneris source update DATASOURCE_ID --config CONFIG [--json]
qaneris source delete DATASOURCE_ID [--json]
qaneris source test --config CONFIG [--json]
qaneris source scan --config CONFIG [--json]
qaneris source import-excel FILE [--name NAME] [--workspace WORKSPACE_ID] [--json]

qaneris ask QUESTION [--workspace WORKSPACE_ID] [--datasource DATASOURCE_ID] [--max-rows N] [--stream] [--json]

qaneris acceptance phase1 [--question-class A-F] [--repeat N] [--no-trace] [--json]
qaneris acceptance ask --suite phase1 [...同上]

qaneris shell [--history FILE | --no-history] [--no-banner]

CLI 是本地编排层：它只调用 `QanerisService`，不改造成 HTTP Client(API客户端)，也不直接访问 Catalog / `ManagedCredentialStore` / `CertificateValidator` / SecretResolver / `TLSMaterializer` / Adapter / Neo4j。

### 7.1 Secret Input Contract(Secret 输入契约，RS-CLI-02)

Secret 原文不得进入 argv / shell history / process list / JSON config / 普通 CLI 输出。因此命令树里**不存在**任何携带 secret 原文的参数：没有 `--password VALUE`、`--token VALUE`、`--api-key VALUE`、`--secret VALUE`、`--value VALUE`、`--private-key-password VALUE`。两种唯一的输入通道：

| 通道 | 用法 | 行为 |
| --- | --- | --- |
| 隐藏交互输入 | 省略 `--stdin`，stdin 是 TTY | `getpass.getpass(..., stream=sys.stderr)`，不回显；提示本身写 stderr，`--json` 时 stdout 仍只有一个 JSON 文档 |
| 显式管道 | 加 `--stdin`（或 `--private-key-password-stdin`） | 读取 stdin，最多剥掉一个结尾 `\n` 或 `\r\n`；绝不 `strip()`（`" secret "` 是合法 secret）；上限 65,536 字符 |

- 非 TTY 且未给显式 `--stdin` → 用法/配置错误（exit 2），不会静默地把重定向的文件当成密码读走。
- `credential add --kind` 只接受 text kind；`ca_certificate` / `client_certificate` / `client_private_key` 必须走 `certificate` 命令，不能借 `--kind` 绕过文件边界。
- `certificate add-client` 的无密码情形传 `private_key_password=None`，CLI 不猜测私钥是否加密；`--private-key-password-prompt` 与 `--private-key-password-stdin` 互斥。
- 证书/私钥经文件路径导入：`--private-key` 接受的是**路径**（不是密钥正文），单文件上限 1 MiB，读入内存后直接交给 Application Service，不落临时磁盘。`certificate inspect` / `delete` 先回读 kind，对 text secret 返回 `credential_kind_mismatch`，因此 `certificate` 命令不会变成第二个无类型凭据入口。
- exit code：0 = 命令完成；1 = 命令执行了但失败；2 = 用法/配置错误。`--json` 的 stdout 恰好一个 JSON document，提示与附带输出一律走 stderr。

### 7.2 Secure Datasource Lifecycle Commands(安全数据源生命周期命令，RS-CLI-02)

`test-one` / `create` / `update` 共用一个配置文件，格式与批量 `source test/scan` 完全相同（`SecureDatasourceCreate`），不引入 CLI 专用的 update 配置格式，也不接受内联 secret（`password` / `token` / URL 内嵌密码等一律拒绝并要求改用 `SecretReference`）。文件必须恰好描述一个数据源，多个会被拒绝而不是静默挑选。

| 命令 | Application Service 调用 | 关键规则 |
| --- | --- | --- |
| `source test-one --config C` | `test_secure_datasource()` | 只测候选连接，不创建、不扫描任何数据源 |
| `source create --config C` | `create_secure_datasource()` | 只做 test → save；**不自动 scan**，status 停在 `created`（`Next:` 提示显式 `scan-one`） |
| `source update ID --config C` | 先 `inspect_datasource(ID)`，再 `update_secure_datasource()` | `name` / `kind` / `workspace_id` 不可变；配置与现有数据源不一致时为 exit 2（`DatasourceConfigError`），不会部分生效；**不自动 scan**，`--json` 返回 `scan_required: true` |
| `source delete ID` | `delete_datasource()` | 没有 `--force`；图的删除顺序由 Application Service 拥有 |

`source test-one/create/update/delete` 与既有 `source list/show/scan-one` 一样是薄投影：CLI 不自行查询 Catalog，也不复述服务端拥有的顺序规则。

`qaneris ask` 是一条非交互式命令：one command → one `AskRequest` → one `AskResponse`（同步）或一条 `AskEvent` 序列（`--stream`）；两条路径共用同一个 `AskRequest`，因此参数校验完全一致。CLI 只调用 `QanerisService.ask()` / `QanerisService.ask_stream()`，不触碰 intent/grounder/planner/compiler/executor/模型网关；`--datasource` 只接受 datasource id（不猜 name）；`--max-rows` 的合法范围由 `AskRequest` 契约校验。

四种 Ask 模式：

| 命令 | stdout | 退出码 |
| --- | --- | --- |
| `ask "Q"` | human：completed 展示 Status / Datasource / Scan version / Rows / Truncated / 结果表格（仅显示截断，不改数据）/ `evidence.display_command` / 简要 Plan；`clarification_required` 输出 "Clarification required" 与公开澄清内容；failed 只输出稳定错误码，无 traceback | 0 / 1 |
| `ask "Q" --json` | stdout 恰好一个 JSON document，是 `AskResponse` 的稳定字段投影（status / question / answer / business_query / clarification / plan / result / evidence / error）；`analysis` 与 `recommended_questions` 刻意排除 | 0 / 1 |
| `ask "Q" --stream` | human 轨迹：每个真实 `AskEvent` 一行 `[stage] headline (事实...)`，`clarification_required` 追加公开澄清问题，`result_ready` 追加 Answer / 结果表格 / 安全查询展示；不伪造阶段、不显示私有 reasoning、不显示 secret | 0 / 1 |
| `ask "Q" --stream --json` | 严格 JSONL：每行是该 `AskEvent` 自身序列化结果（无包装字段、无第二套 schema），`done` 恒为最后一行 | 0 / 1 |

- exit code：completed 与 clarification_required → 0（澄清是产品结论，API/MCP 同样按正常响应返回）；failed 与产品/运行时异常 → 1；usage/配置错误 → 2。
- `--stream --json` 下 stdout 只承载事件：driver/provider/日志输出统一重定向到 stderr；usage 错误发生在任何事件之前，因此其 JSON 文档也走 stderr，stdout 保持为空。
- 流式失败由服务的 `error` 事件加终止 `done` 表达，CLI 不打印 traceback，也不补造 `done`。

CLI 只能编排 Application Service。JSON/JSONL 机器输出用于验收；不得成为新的 Source of Truth(事实来源)。`source import-excel` 的 human/JSON 输出、错误码与 exit code 契约见 `excel-ingestion.md` §16.1；`--json` 的 stdout 只允许单一 JSON document。

`source list/show/scan-one` 同样是薄投影：`list` 与 `show` 只读，`scan-one` 调用 `QanerisService.scan_datasource()` 后经 `inspect_datasource()` 报告新的 active snapshot version。三者都只发布公开字段（ID / Name / Kind / Driver / Status / Workspace，以及 Scan version / Last scan status / Dataset count），CLI 不自行查询 Catalog，也不接受 `--password` / `--token` 等交互式凭据录入。

### 7.3 Interactive Session(交互式会话)

`qaneris shell` 是前面所有命令的交互式外壳（inline TUI：横幅、常驻状态行由终端 UI 绘制，命令自身的输出留在终端原生 scrollback，不使用备用屏）。它**不新增任何产品行为**：

- 每行经 shell 分词后交给与一次性命令完全相同的 `main()` 入口，因此输出、退出码与错误规则逐字一致；shell 不解析命令、不渲染命令专属结果、不接触 Application Service / Catalog / Adapter。
- 会话内建词只有 `exit` / `quit` / `help` / `clear`（`?` 是 `help` 的别名），且仅在该词不是已注册命令时才生效，因此内建词永远不会遮蔽命令树中的同名命令。每一行在分词后会剥掉行首重复的程序名，因此从 README 或上级 scrollback 复制来的 `qaneris source list` 与直接输入 `source list` 等价（程序名不可能是子命令，剥离不会改变任何合法行的含义）；只输入程序名时返回帮助文本。会话内再次输入 `shell` 不会嵌套第二层会话，而是打印 `qaneris shell` 的帮助 —— 外层会话已经确定了历史与横幅选项，再跑一次无法兑现这些选项。
- `argparse` 用 `SystemExit` 表达 `--help` 与用法错误（退出码 0 / 2）；shell 把它转成该命令的退出码，**一次打错不会结束会话**。命令未预期的异常按类型上报，不显示任意消息（与一次性边界一致）。
- 单条命令的退出码显示在状态行，**不作为会话进程的退出码**；会话正常结束时退出码为 0。Ctrl-C 取消当前输入行而不退出，Ctrl-D 退出。
- 非终端 stdin 一律拒绝（退出码 2 并提示改用一次性命令），避免脚本隐式进入交互模式；缺少可选依赖时提示 `pip install -e '.[shell]'`，不输出 traceback。
- **Secret 边界不变**：shell 不提供任何携带 secret 原文的选项。`credential add` / `certificate add-client` 的交互式输入仍走 `getpass`（直读控制终端），不经过 shell 的自绘输入行，因此**不会进入 readline 历史文件**。§7.1 的 Secret Input Contract 对交互式会话同样适用。
- 命令历史写入 `.tools/shell_history`（目录 0700 / 文件 0600，`.tools/` 已被 Git 忽略），可用 `--no-history` 关闭；历史不可用时退回内存历史，不拒绝启动。
- 会话进程会把 Neo4j driver 的 DBMS notification logger（`neo4j.notifications`）提升到 ERROR：这类记录描述的是服务器而非命令，在一次性报告中无害，但在会话里会横穿提示符。该设置仅作用于会话进程，API 与一次性命令行为不变。
- **展示层约定**（仅影响观感，不改变任何命令输出）：横幅卡片随终端宽度自适应——两个分区在放得下时并排、放不下时纵向堆叠，分隔线由 `rich` 按终端宽度绘制而非固定长度；颜色一律用 ANSI 命名色（跟随用户主题），品牌青为唯一强调色，`0` 退出码为绿、`1` / `2` 为红。状态行覆盖 `prompt_toolkit` 默认给 `bottom-toolbar` 的 `reverse`，否则退出码配色会被整体反色。横幅内的 model / 凭据目录 / catalog 是**数据而非 markup**，一律以文本追加（含方括号的路径因此原样显示），家目录下的路径按 `~` 缩写（仅显示层缩写，取值不变）。

- 依赖 `prompt_toolkit` / `rich` 为**可选依赖**（`[shell]` extra，独立入口 `qaneris-shell`）；核心安装、API、MCP 与所有一次性命令不依赖它们。

## 8. MCP + Skill Contract(MCP与Skill契约)

当前产品顺序固定为 `RS-CRED-01B → RS-CLI-02 → RS-EXCEL-02 → RS-MCP-01 → RS-SKILL-01 → RS-WEB-01 → RS-VIZ-01`。Skill 必须在 MCP Product Contract 稳定后收口；Web 的 Secure Datasource UI 必须复用同一 Credential / Secure Datasource API，不允许另造连接协议。


MCP 已有的 `ask_data` 等工具已完成 Roadshow 产品化（RS-MCP-01 DONE）。

**依赖边界**：MCP interface 只调用 `QanerisService` 公开方法。它不读 `Catalog`、不持有 model、不构造 `ManagedCredentialStore` / `SecretResolver` / `TLSMaterializer` / Adapter / Graph，也不调用模型 SDK。这些能力全部留在 Application Service 内部，MCP 只做参数整形、进度转换与错误转换。`tests/unit/interfaces/mcp/test_mcp_boundary.py` 会读取 server 源码做机械校验，新增 Tool 若越界会在测试中失败。

**凭据边界**：MCP 只接收 `SecretReference`（`provider` + `identifier`），**不得接收 password、Token、Private Key 或 Certificate 原文**。不存在任何凭据创建 / 上传 Tool（无 `create_password` / `upload_certificate` / `upload_private_key` / `credential_add`）。Secret 必须先经 CLI 或 HTTP API 创建，MCP 再按引用消费。legacy `register_datasource(name, kind, driver, connection_json)` 已删除，不留第二条隐藏通道；`register_sqlite` 作为兼容便利工具保留，但内部构造 `SecureDatasourceCreate` 并走正式 Secure 契约。

**Secure Datasource 输入**：`test/create/update` 接收结构化 `ConnectionProfile`（FastMCP 依据 Pydantic 契约生成嵌套 schema），不接收 `connection_json` 字符串。`test` 只测试、不保存 / 不 scan / 不写图；`create` 与 `update` 完成后都停在 `status=created`，scan 仍是唯一显式扫描入口；`delete` 只调用 `service.delete_datasource()`，图删除与 secret 回收仍属 Application Service。

**Progress**：MCP Progress 消费 `QanerisService.ask_stream()` 的统一 `AskEvent`，不建立第二套事件 schema。`ask_data` 是 async Tool，通过 AnyIO worker-thread bridge 驱动同步 generator，每个 `AskEvent` 触发一次 `ctx.report_progress(progress=float(event.sequence), message=event.event_type.value)`。**不传 `total`**（Ask 分支可能是 completed / clarification / error，事件总数事先不固定，不得伪造）；message 只含 `event_type.value`，不塞完整 payload。同一 Ask 只执行一次：终态响应取自 `result_ready` / `clarification_required` / `error` 事件，绝不二次调用 `service.ask()`。`clarification_required` 属正常产品结论（Tool `isError=false`）；`error` 保留稳定 Qaneris code 并以 MCP Tool error 返回，未知异常只暴露 exception type。

**Skill（RS-SKILL-01 DONE）**：Skill 只描述如何正确调用 MCP，不拥有第二套数据源选择、查询规划或安全规则。它把 Secure Datasource 固定为 `test_secure_datasource` → `create_secure_datasource` → `scan_datasource`（Update 为 `test` → `update` → `scan`），把 `created` 明确标注为不可问数状态，把 schema 工具定位为排歧诊断而非固定前置步骤，把 Progress 解释为执行阶段而不是思维链，并把 `clarification_required` 当作正常产品结论。回答事实来源固定为 `answer` / `result` / `evidence`。凭据侧与 MCP 同界：只传 `SecretReference`，不接收也不回显任何 secret 原文；用户没有引用时，引导其先经 CLI、HTTP API 或 Web 创建凭据。契约由 `tests/unit/skill/test_skill_contract.py` 守卫。

## 9. Datasource Connection Boundary(数据源连接边界)

Credential / Certificate / Datasource 的详细设计只维护在 `connection-model.md`。所有产品入口统一走：

```text
Secret Upload / Certificate Upload
→ opaque SecretReference
→ ConnectionProfile
→ QanerisService Secure Datasource Contract
→ connection test
→ save / scan
```

Web 的正式 Datasource Wizard(数据源向导)固定为：Driver → Endpoint → Authentication → TLS/Certificate → Test Connection → Save → Scan → Ready。任何产品入口都不得把敏感材料写入普通 Catalog、Neo4j、Prompt、Trace、JSON 输出、MCP 工具参数或日志。

### 9.1 Web Secure HTTP Surface

Web datasource API 由 `qaneris/interfaces/api/datasources.py` 注册，所有生命周期动作只转给同一个 `QanerisService`：

| Method / Path | Service call / response |
| --- | --- |
| `POST /api/datasources/test` | `test_secure_datasource()`；只测试连接，不写 Catalog / Graph / Credential Store |
| `POST /api/datasources/secure` | `create_secure_datasource()`；`201` + `created`，不自动 scan |
| `GET /api/datasources/{id}` | `inspect_datasource()`；只返回 `DatasourceDetail` 安全投影 |
| `PUT /api/datasources/{id}/secure` | `update_secure_datasource()`；保留原身份字段，成功后为 `created`，需要显式 scan |
| `DELETE /api/datasources/{id}` | `delete_datasource()`；成功 `204` 空响应 |
| `POST /api/datasources/{id}/scan` | 现有显式 scan 入口 |
| `GET /api/tls-capabilities` | `list_tls_capabilities()`；只投影四种公开能力，唯一事实来源为 `TLS_DRIVER_MATRIX` |

`GET /api/adapters` 同样经由 `QanerisService.list_supported_adapters()`。legacy `POST /api/datasources` 保留兼容，React secure workflow 不调用它。Credential 与 CA / client identity 上传继续复用 RS-CRED-01B 的接口，无 Credential list endpoint。React production 使用 `web/frontend/dist/`；无 Login/Auth 的路演部署必须位于 controlled network / VPN / reverse proxy access control 之后。

## 10. Non-goals Before Roadshow(路演前非目标)

- 不要求 Agent Runtime / Task DAG 成为 Roadshow 主链。
- 不要求跨数据源 Join。
- 不要求 Vector / Hybrid / Reranker / Enterprise RAG 在路演前完成。
- 不为了 Streaming 或 Visualization 绕过 Grounding、QueryPlan、Validation、Revision Fence 和只读执行边界。
