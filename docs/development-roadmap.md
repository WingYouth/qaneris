# SmartData Development Roadmap(开发路线图)

**Updated(更新)：2026-09-24**  
**SmartData source baseline：main @ 94d8e58dd1ca74cb7c0810f0eaacdeb92d5c4844**

**Test-data baseline：JingJIang96200/NLQuery-Test-Dataset main @ cb1c520dbc150412e271049cd45298a99c5d8a16**
**Current Priority(当前优先级)：Product Roadshow E2E。Credential Upload → Secure CLI → Excel 产品链回归 → MCP → Skill → Web → Visualization 均已完成实现；真实 Web 验收受 Neo4j/模型环境约束。16-DB / TLS 真实验收延后到数据库部署到服务器后执行。Phase 2 Retrieval Intelligence 暂不作为路演阻塞项。**

状态规则：`DONE` 只表示源码和仓库证据已经存在；`PARTIAL` 表示已有基础但缺路演要求；`TODO` 表示当前源码中尚未实现；`BLOCKED` 表示存在明确外部阻塞。计划项不得提前标记完成。

**Roadshow 范围决定：本版本不开发 User Login / Session / JWT / OAuth / RBAC。`workspace_id` 继续作为业务作用域参数；Web/API 路演环境必须放在受控网络、VPN 或反向代理访问控制之后，不作为开放公网服务。**

## 1. Current Verified State(当前已核实状态)

| Capability(能力) | Status | Source-backed fact(源码事实) |
| --- | --- | --- |
| Phase 1 Trusted Query Engine(可信查询引擎) | DONE | `SmartDataService.ask()` 已形成 Question → Intent → Retrieval → Grounding → QueryPlan → Validation → Read-only Execution → Typed Result + Evidence 主链。 |
| Phase 1 real E2E(真实端到端) | DONE / historical baseline | 仓库保留真实 LLM + Neo4j + SQLite 固定六类问题、3 次重复，共 18/18 符合预期的持久验收报告；这不是系统总体准确率 100%。 |
| Model gateway(aiyallm 模型网关) | PARTIAL | 代码已迁移到 aiyallm；仓库没有一份迁移后成功的真实模型回归记录，现有 18/18 属迁移前验收基线。 |
| Scan → Neo4j Enterprise Data Graph | DONE | 多数据源 ScanRun、每数据源独立 ScanSnapshot、Neo4j 发布和 Catalog ↔ Graph 校验已实现。 |
| Adapter Registry(适配器注册) | DONE | `smartdata/adapters/registry.py` 当前注册 21 个 driver；注册不等于 21 个数据库都已完成真实问数 E2E。 |
| 16 service DB test fixtures(16个服务型测试库) | DONE in test repo | `NLQuery-Test-Dataset` 已准备 PostgreSQL、MySQL、SQLServer、MongoDB、Redis、CouchDB、Cassandra、ClickHouse、TimescaleDB、Elasticsearch、OpenSearch、InfluxDB、Neo4j、Milvus、Qdrant、Weaviate；另有容器化 SQLite，共 17 个测试目标。 |
| Roadshow server deployment(路演服务器部署) | DEFERRED UNTIL SERVER DEPLOYMENT | 16 个服务型数据库的真实部署、TLS handshake、SmartData connection/scan 与 Neo4j 回读验收延后到服务器环境执行；当前 Product Surface 开发不以本地 16 库验收为前置条件。 |
| SSL/TLS certificate lifecycle(证书生命周期) | PARTIAL / RS-CRED-01A + RS-CRED-01B + RS-CONN-01A + RS-CONN-01B + RS-CLI-02 + RS-WEB-01 DONE | `TLSConfig` 与 `SecretReference` 已有；Managed CredentialStore、证书校验、Secure Datasource 生命周期和 16 Driver TLS Matrix 已实现；Web Wizard 复用 credential/certificate API，并由 `GET /api/tls-capabilities` 驱动能力控件。仍缺真实服务器部署与 TLS 握手验收（RS-TLS-01 / RS-DB-02）。代码支持 16 Driver ≠ 真实 TLS 验收完成。 |
| API Ask | DONE | FastAPI 已有 `POST /api/ask`，直接进入 `SmartDataService.ask()`；RS-STREAM-01B 增加 `POST /api/ask/stream`，以 `text/event-stream` 直接传输 `SmartDataService.ask_stream()` 的 `AskEvent`（不复制 schema、不改写 payload）；另有 `POST /api/datasources/import-excel` 浏览器 multipart Excel 上传边界（只调用 `SmartDataService.import_excel()`，临时文件 repository 外且必清理，响应只含产品安全字段）。 |
| CLI product surface | DONE / RS-CLI-01A + RS-CLI-01B + RS-CLI-02 DONE | RS-CLI-01A + RS-CLI-01B 已完成问数、Streaming、已有数据源 list/show/scan、批量 test/scan 与 Excel 导入；RS-CLI-02 补齐最终 Roadshow Secure CLI：`credential add/show/delete`、`certificate add-ca/add-client/inspect/delete`、`source test-one/create/update/delete`。全部命令继续只编排 Application Service，secret 只经隐藏输入或显式 `--stdin` 进入，不出现任何携带原文的 argv 参数。 |
| MCP product surface | DONE / RS-MCP-01 | stdio MCP Server 的每个 Tool 只调用 `SmartDataService` 公开方法，不直接读 Catalog / model；legacy `register_datasource(connection_json)` 已删除，`register_sqlite` 改走 Secure Datasource 契约（`status=created`，不自动 scan）；新增 `inspect_datasource` / `test_secure_datasource` / `create_secure_datasource` / `update_secure_datasource` / `delete_datasource`，只接收 `SecretReference`，无任何凭据创建或上传 Tool；`ask_data` 改为 async 并单次消费 `ask_stream()`，把每个 `AskEvent` 1:1 映射为标准 MCP progress（`progress=sequence`、`message=event_type.value`、不带 `total`），终态响应取自事件而非二次 `ask()`。真实 stdio 协议验收（含 `progress_callback`）通过。 |
| Skill product surface | DONE / RS-SKILL-01 | `skill/SKILL.md` 与 `skill/references/behavior-examples.md` 已对齐当前 MCP Product Contract：只使用现行 Tool 名（`ask_data` / `test_secure_datasource` / `create_secure_datasource` / `update_secure_datasource` / `scan_datasource` / `list_datasources` / `register_sqlite`），legacy `register_datasource` 已从指导中删除；Secure Datasource 固定为 Test → Create → Scan（Update 为 Test → Update → Scan），并说明 `created` 不是可问数状态；MCP 只消费 `SecretReference`，缺引用时通过 CLI / HTTP / Web 安全入口创建凭据；回答事实来源固定为 `answer` / `result` / `evidence`，Progress 是执行阶段而非思维链。`tests/unit/skill/test_skill_contract.py` 做静态契约守卫。 |
| Web product surface | DONE / RS-WEB-01 + RS-VIZ-01 | React 工作台默认进入智能问数，提供智能问数/数据源/Excel 导入导航、SSE Ask 与安全事件 Trace、Secure Datasource test/create/update/delete/scan HTTP 生命周期、TLS capability API、凭据/证书上传 Wizard、Result/Evidence、受控 SVG 图表，以及 Vite production build 和 FastAPI/Docker 静态服务。 |
| Excel ingestion(Excel导入) | DONE / EXCEL-01A + EXCEL-01B + EXCEL-01C DONE | `SmartDataService.import_excel()`、确定性校验、不可变 SQLite、Scan/Neo4j 与导入后的问数已完成；EXCEL-01C HTTP multipart 与基础 Web 流程已有真实 E2E。RS-WEB-01 已将其编排进完整工作台，导入成功可直接转到对应问数范围。 |
| Result Visualization(结果可视化) | DONE / RS-VIZ-01 + RS-VIZ-02 local acceptance | Typed Result 在浏览器经确定性规则生成受控 ChartSpec，React SVG 显示柱状图、折线图、饼图；真实 Neo4j/SQLite/HTTP/SSE/Chrome 链路 57/57 通过。外部 OpenRouter 429 与 Docker Hub 拉取超时另见 `docs/rs-viz-02-acceptance.md`。 |
| Structured Trace + SQL display | DONE for Web trace projection | React 使用 `POST /api/ask/stream` 展示公开阶段事实与安全 `display_command`；不投影参数或 raw payload。 |
| Streaming(流式传输) | DONE / RS-STREAM-01B + RS-WEB-01 | Web 以 POST + fetch/ReadableStream 消费 SSE，校验帧名、事件字段与递增序号，要求真实 done；completed / clarification / failed 结果均从原 stream 终态事件读取，不重复 Ask。 |
| Demo video(演示视频) | TODO | 需要在 Roadshow E2E 固定后录制。 |

## 2. Roadshow Release Scope(四天路演版本范围)

| Work Package | Target(目标) | Exit Criteria(完成条件) |
| --- | --- | --- |
| RS-DB-01 16-DB Deployment | 将 `NLQuery-Test-Dataset` 的 16 个服务型数据库部署到路演服务器 | **DEFERRED UNTIL SERVER DEPLOYMENT。** 16 个服务健康；只读账号可从 SmartData 运行环境连接；部署配置不把生产秘密提交 Git。当前产品开发不等待该项。 |
| RS-TLS-01 TLS / Certificate | 16 个服务型库中 14 个完成 SSL/TLS 路演连接 | **DEFERRED UNTIL SERVER DEPLOYMENT。** 在服务器环境完成 14 个真实 TLS handshake + SmartData connection test + scan；另外 2 个豁免目标必须根据真实协议/driver 支持明确记录，不能凭空指定。 |
| RS-CRED-01A Managed Credential Core | 提供受管凭据与证书的加密存储、校验与安全引用底座 | 状态：DONE。`SecretProviderKind.MANAGED`、`ManagedCredentialStore`（AES-256-GCM + AAD，仓库外 `0700` 目录、`0600` 单文件、原子写）、`CertificateValidator`（PEM/DER 归一化、有效期与用途校验、私钥 PKCS#8 归一化、cert/key 配对）、`CredentialService`（校验 + 归一化 + 引用保护删除）、`ManagedSecretProvider` 均已实现；112 个新单元测试通过，`ruff check .` 全绿。 |
| RS-CRED-01B Product Upload Boundary | 把凭据与证书上传暴露为产品入口 | 状态：DONE。固定 4 个 HTTP 产品入口：`POST /api/credentials`（JSON，仅 password / token / api_key / client_private_key_password）、`POST /api/certificates/ca`（multipart `file`）、`POST /api/certificates/client-identity`（multipart `certificate` + `private_key` + 可选 transient `private_key_password`）、`GET`/`DELETE /api/credentials/{secret_id}`。`ManagedCredentialPublic` 以 `secret_id` 暴露产品字段而非内部 `ManagedSecretInfo.id`，`public_credential()` 是唯一投影点。`CredentialService.create_secret()` 接受 `str | bytes`（PEM/DER 都支持），text kind 传 bytes 直接拒绝、不做隐式 decode；新增 `create_client_identity()` 固定顺序为 `normalize cert → normalize key → validate_client_pair → store cert → store key`，store 失败回滚已写入的一半，不留 orphan secret。multipart 单文件上限 1 MiB，64 KiB 分块有界读取，超限 413 `credential_upload_too_large`，证书/私钥只走内存 buffer、不落临时磁盘；声明超限的请求在 multipart 解析前即被拒绝。全部调用走 `SmartDataService` 公开凭据方法，不新增列表 API、不新增 CLI/MCP/Web、不接收 Login/Auth。 |
| RS-CONN-01A Secure Datasource Lifecycle | 用受管凭据统一 create/update/test/delete 数据源生命周期 | 状态：DONE。正式 Application Contract 统一为 secure datasource `test / create / update / delete / list / inspect / scan`；`create` 只做 `test → save`（返回 `status=created`），scan 仍是唯一显式扫描入口。`update` 采用 candidate-first：解析 SecretReference → 真实 connection test → 成功后 `delete old graph → 换 profile → invalidate old scan → 清理不再被引用的 managed secret`；失败时旧 profile、旧活跃 scan、旧图与旧 secret 全部不变。`delete` 先删图再删 Catalog，再回收仅它引用的 managed secret。`invalidate` 在同一事务内停用旧 `scan_snapshot`、清空当前 `dataset/relation/dataset_sample/mapping` 并把 datasource 置回 `created`，历史 snapshot 与 initialization_job 保留。legacy datasource 不自动迁移（`update` 返回 `datasource_secure_profile_required`），但可被 `delete`。 |
| RS-CONN-01B TLS Materialization & Driver Matrix | 建立统一 TLSMaterializer 与逐 Driver TLS 参数映射 | 状态：DONE。`TLSMaterializer.materialize()` 是 context manager：TLS 关闭时不创建临时目录；Driver 接受内容则内存传递，只接受路径则用 `tempfile.mkdtemp(prefix="smartdata-tls-")`（`0700`）写 `0600` 文件（`os.open(O_CREAT\|O_EXCL\|O_WRONLY)` + `flush`/`fsync`，禁用 `Path.write_text()`），`finally` 中 `shutil.rmtree` 覆盖成功、连接失败、scan/query 失败、Adapter 异常与 `KeyboardInterrupt`。物化时对 material 再次执行 `CertificateValidator`，并在开连接前 `validate_client_pair()`。`TLS_DRIVER_MATRIX` 是唯一事实来源，逐 Driver 记录 `strategy` / `custom_ca` / `mtls` / `server_name_override` / `tls13_control`，覆盖 16 个路演 Driver；不支持能力抛 `tls_feature_unsupported`，绝不自动 `verify_server=False`。`to_adapter_connection()` 不再输出 `tls_material`；PEM 正文不入 Adapter dict、不入 URL、不入 Catalog。不设置 `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` / `GRPC_DEFAULT_SSL_ROOTS_FILE_PATH` 等进程级全局变量。本卡只完成 Core + Driver Mapping；14/16 真实 TLS 验收属于 RS-TLS-01 / RS-DB-02。 |
| RS-DB-02 SmartData Multi-DB Acceptance | 用 SmartData 主仓库逐库做 connection + scan + Neo4j 回读验收 | **DEFERRED UNTIL SERVER DEPLOYMENT。** 每库记录 datasource、scan_version、对象/字段/关系计数和失败原因；Adapter 注册不能代替真实验收。 |
| RS-EXCEL-01 Excel End-to-End | `.xlsx` 上传进入现有可信查询主链 | **DONE。** Upload → deterministic validation → managed artifact → immutable SQLite → Scan → Neo4j → Grounding → Query → Result；不创建第二套 Excel 查询引擎。 |
| RS-EXCEL-02 Product-path Regression Acceptance | 在新的 CRED / CONN / TLS Core 与 Secure CLI 完成后复验 Excel 正式产品链 | 状态：DONE。新增总验收入口 `smartdata/scripts/acceptance/excel_product_regression.py`：按固定顺序以 subprocess 真实执行三条既有产品路径 —— A `import_excel_cli`（38/38）、B `cli_ask`（54/54）、C `excel_web_ask`（41/41）—— 子脚本通过 `SMARTDATA_ACCEPTANCE_SUMMARY` 回传 checks，总入口只做汇总与唯一 verdict，绝不复制业务逻辑、绝不伪造 stage。在真实 Neo4j + 真实模型网关上全绿：`.xlsx → CLI import → READY/Neo4j 发布校验 → source show/list → sync Ask → `--stream --json` 严格 JSONL（9 个事件按序 accepted→…→done，含 result_ready/query_ready）→ HTTP multipart upload → 前端模块（Node）E2E`，sync 与 stream 业务结果一致（East 350.5 / West 220.25），未治理 `profit` 仍走 clarification。环境不可用（无 Neo4j / 无模型 / 无 Node）一律报告 BLOCKED 而非 PASS。新增 `tests/unit/scripts/test_excel_product_regression.py`（21 项：JSONL parser、事件顺序校验、combined status、report redaction）。不修改 Excel ingestion 既有设计。 |
| RS-CLI-01 Product CLI Base | 提供正式 `smartdata ask` 与 Roadshow 基础命令体验 | **DONE：** `ask` 四种模式 + `source list/show/scan-one` + `source test/scan` + Excel 导入均已实现并只调用 Application Service。该工作包不包含 Secure Datasource / Managed Credential 产品管理。 |
| RS-CLI-02 Secure Product CLI | 在 RS-CONN-01A / RS-CONN-01B 后完成最终 Roadshow CLI | 状态：DONE。命令树固定为 `smartdata` → `doctor` / `ask` / `credential {add,show,delete}` / `certificate {add-ca,add-client,inspect,delete}` / `source {list,show,test-one,create,update,delete,scan-one,test,scan,import-excel}` / `acceptance`。CLI 仍是本地编排层，只调用 `SmartDataService`，不引入 HTTP client、MCP、Skill、Web 或 Login/Auth。**Secret 输入**：text secret 只经 `getpass.getpass(..., stream=sys.stderr)` 隐藏提示或显式 `--stdin`（最多剥掉一个结尾 `\n` / `\r\n`，绝不 `strip()`，上限 65,536 字符）；非 TTY 且未给 `--stdin` 视为用法错误（exit 2），不静默读管道；不存在任何携带 secret 原文的参数（无 `--password` / `--token` / `--api-key` / `--secret` / `--value` / `--private-key-password VALUE`）。**Certificate**：`add-ca` 读单个 PEM/DER 文件，`add-client` 读 `--certificate` / `--private-key` 两个文件路径，可选 `--private-key-password-prompt` 与 `--private-key-password-stdin` 互斥；无密码即传 `private_key_password=None`，不猜测密钥是否加密；单文件上限复用 `CREDENTIAL_FILE_MAX_BYTES = 1 MiB`；`inspect` / `delete` 先回读 kind，非证书类返回 `credential_kind_mismatch`。**Secure source**：`test-one` 只调用 `test_secure_datasource`，`create` 只调用 `create_secure_datasource` 且不自动 scan（status 停在 `created`），`update` 先 `inspect_datasource` 校验 `name` / `kind` / `workspace_id` 不可变再调用 `update_secure_datasource`（不匹配为 exit 2，不自动 scan），`delete` 无 `--force`；复用既有 `SecureDatasourceCreate` 配置格式，不新增 CLI 专用 update 格式，不接受内联 secret。**输出**：exit code 固定 0 = 完成、1 = 执行但失败、2 = 用法/配置错误；`--json` 的 stdout 恰好一个 JSON 文档，提示与附带输出一律走 stderr。 |
| RS-SKILL-01 Skill | Skill 与当前 Product Contract 对齐 | 状态：DONE。Skill 只描述如何正确调用 MCP，不拥有第二套数据源选择、查询规划或安全规则。**Ask 路径**：`list_datasources` 确定来源（恰好一个 ready 时直接使用，不先索要表名）→ `ask_data(原始问题)`；schema 工具（`get_schema_context` / `summarize_schema` / `search_dataset` / `list_relations` / `list_mappings`）定位为排歧诊断工具，不是每次 Ask 的前置步骤。**数据源路径**：SQLite 用 `register_sqlite` → `scan_datasource`；其他 Driver 固定 `test_secure_datasource` → `create_secure_datasource` → `scan_datasource`，Update 为 `test` → `update` → `scan`（update 使旧 scan 失效）；`created` 明确不是可问数状态；只有用户明确要求才 `delete_datasource`，不因连接 / scan / ask 失败自行删除。**凭据边界**：`connection_profile` 内只放 `SecretReference`（`managed` / `environment` / `file`），绝不接收或回显 password / token / API key / certificate PEM / private key / private key password；缺引用时引导用户先经 CLI 或 HTTP API 创建凭据拿到 `sec_...`，Web 标注为尚未可用；不存在任何凭据创建 / 上传 Tool。**回答事实来源**：`answer` / `result` / `evidence`，不再依赖内部 `analysis`；空结果不解释为 0，`truncated` 必须说明，不跨数据源拼接。**Progress**：`accepted → … → done` 是执行状态，不是思维链；无 progress UI 不等于失败。普通问数失败不改写自创 SQL 绕路，`sql` 仅在用户明确给出或要求单条只读查询时使用。删除过时的 SQLite-only 规划能力描述。新增 `tests/unit/skill/test_skill_contract.py` 静态契约测试（30 项），不 snapshot 全文。 |
| RS-MCP-01 MCP Productization | MCP 与统一 Ask、Secure Datasource、进度事件对齐 | 状态：DONE。MCP interface 不再直接读 Catalog / model，全部 Tool 只调用 `SmartDataService`；`register_datasource(connection_json)` 已删除，`register_sqlite` 改为构造 `SecureDatasourceCreate` 走正式 Secure 契约且不自动 scan；新增 `inspect_datasource` 与 secure datasource `test/create/update/delete`，其中 `test` 不保存 / 不 scan / 不写图，`create` 与 `update` 均停在 `status=created`，`delete` 只调 `service.delete_datasource()`；MCP 只接收 `SecretReference`（`provider` + `identifier`），不存在任何凭据创建或上传 Tool。`ask_data` 改为 async：单次消费 `ask_stream()`，经 AnyIO worker-thread bridge 把每个 `AskEvent` 1:1 映射为 `ctx.report_progress(progress=float(event.sequence), message=event.event_type.value)`，不传 `total`，message 不含 payload；终态响应取自 `result_ready` / `clarification_required` / `error`，绝不二次调用 `ask()`；澄清按正常结果返回（`isError=false`），错误保留稳定 SmartData code，未知异常只暴露类型。真实 stdio 协议验收（含真实 `progress_callback` 与无 callback 两种客户端）通过。新增边界测试机械校验 Tool schema 不含 raw-secret 输入且 server 源码不越过 service 边界。 |
| RS-WEB-01 Unified Ask Workspace | Web 完成路演工作台 | **DONE（实现与定向验收完成；真实 Neo4j/模型流式 E2E 需在可用环境验收）**。React 中文工作台、Secure Datasource API/Wizard、凭据/证书 UI、POST SSE、Trace、Result/Evidence、React dist 部署与 Docker 构建已加入。 |
| RS-VIZ-01 Visualization / RS-VIZ-02 Acceptance | 查询结果可视化及运行环境验收 | **DONE（确定性图表与真实本地 Neo4j/SQLite/Chrome 验收 57/57）**。Typed Result → deterministic chart policy → controlled ChartSpec → React SVG。外部模型账户额度与 Docker Hub 网络仍为外部验收边界，详见 `docs/rs-viz-02-acceptance.md`。 |
| RS-STREAM-01 Unified Streaming | Web / CLI / MCP / Skill 共享一个结构化 Ask Event Contract | **DONE**：CLI/API/MCP/Web 都消费统一 `AskEvent`；Web 检查 SSE framing / event contract / sequence / done，并从终态帧取 response，不重复执行 Ask。 |
| RS-DEMO-01 Roadshow Demo | 固定演示数据、问题、脚本与视频 | 路演演示从数据源/Excel → Scan/Graph → Ask → Trace/SQL → Visualization 完整跑通并录制。 |

## 3. Roadshow Critical Path(路演关键路径)

优先顺序不是继续扩张 Phase 2，而是先稳定路演契约：

```text
Core Foundation
RS-CRED-01A (DONE)
→ RS-CONN-01A (DONE)
→ RS-CONN-01B (DONE)
        ↓
Product Surface
RS-CRED-01B (DONE)
→ RS-CLI-02 Secure Product CLI (DONE)
→ RS-EXCEL-02 Product-path Regression (DONE)
→ RS-MCP-01 (DONE)
→ RS-SKILL-01 (DONE)
→ RS-WEB-01 (DONE)
→ RS-VIZ-01 (DONE)
        ↓
Product Roadshow E2E
        ↓
Server Deployment Acceptance
RS-DB-01 + RS-TLS-01 + RS-DB-02
        ↓
Final Demo / Video
```

当前执行原则：进入 Product Roadshow E2E；16-DB / TLS 验收不在本地继续消耗开发时间，待数据库部署到服务器后统一执行。服务器验收仍是最终 Release Gate，但不是 CRED-01B / CLI / MCP / Skill / Web / Visualization 的开发前置条件。所有产品入口最终仍必须回到同一个 `SmartDataService` / Application Contract。

## 4. Release Gates(发布门)

- **不把 21 Adapter 注册说成 21 库真实验收。**
- **不把测试数据仓库准备完成说成服务器已部署。**
- **不把 TLSConfig 或 RS-CRED-01A 存在说成完整证书产品生命周期已完成。**
- **不把 RS-CONN-01B 的 16 Driver TLS 矩阵与单元测试说成 14/16 真实 TLS 验收完成；真实握手验收属于 RS-TLS-01 / RS-DB-02。**
- **不把 RS-CLI-01 基础命令完成说成最终 Secure CLI 已完成；安全连接 CLI 必须建立在 CONN 正式契约之后。**
- **不为 Roadshow 临时新增 Login/Auth 系统；无登录的 Web/API 必须依赖部署侧访问控制。**
- **不把历史 18/18 固定验收集说成系统总体准确率 100%。**
- **不把 `QueryTrace` 解释成模型私有思考过程。产品只展示结构化执行事实：Intent 摘要、Retrieval/Grounding 结果、QueryPlan、SQL/Native Query、Execution、Evidence 和 Error。**
- Roadshow Release 完成后，Phase 2 Vector / Hybrid / Reranker / RAG 再恢复为主开发线；它们不能改变 Grounding 后的物理确定性边界。
