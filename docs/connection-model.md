# Qaneris Connection & TLS Model(数据源连接与SSL/TLS设计)

本文是 Roadshow Credential / Certificate / Secure Datasource / TLS 的唯一详细设计来源。Roadshow 明确不开发 User Login / Session / JWT / OAuth / RBAC；`workspace_id` 保持业务作用域参数。

## 1. Current Source-backed State(当前源码事实)

当前代码已经存在结构化安全连接模型：

- `ConnectionProfile`：driver、deployment_mode、endpoint、authentication、tls、options。
- `AuthenticationConfig`：password / token / api_key / private_key / service_account 使用 `SecretReference`。
- `TLSConfig`：`enabled`、`verify_server`、`server_name`、`minimum_version`、`ca_certificate`、`client_certificate`、`client_private_key`、`client_private_key_password`。
- `SecretProviderKind` 现为 `environment`、`file` 和 `managed`。
- `SecretResolver.materialize()` 在运行时解析 Secret Reference，形成 `ResolvedConnection`；`to_adapter_connection()` 只在 Adapter 边界把连接物化。
- `Catalog.create_secure_datasource()` 保存 `connection_profile_v1` 和 Secret Reference，不保存 Secure Contract 里的 secret 原文。
- `QanerisService.create_secure_datasource()` 会先 materialize 并 `test_connection()`，成功后**只保存**数据源（`status=created`）；它不再顺带 scan，`scan_datasource()` 是唯一扫描入口。

RS-CRED-01A 之后，受管凭据核心已经存在：

- **Managed CredentialStore**：`qaneris/connections/managed_store.py`。每个 secret 一个文件，AES-256-GCM 加密、每次新建 nonce，并把 secret 身份与类型绑定为 AAD，使密文不能被移到别的 secret 或类型下；存储目录由 `QANERIS_SECRET_STORE_DIR` 指定且必须在仓库之外（`0700`），单文件 `0600`；写入走同目录临时文件 + `os.replace` 的原子路径；`QANERIS_MASTER_KEY` 必须是 32 字节 URL-safe Base64，缺失或非法一律 fail closed，不会自动生成或回退弱默认值。
- **Certificate Validator**：`qaneris/connections/certificate_validator.py`。PEM/DER 都接受并统一归一化为 PEM；校验解析、当前有效期、CA/client 用途合理性；私钥支持 PKCS#8 与 TraditionalOpenSSL 并归一化为 PKCS#8 PEM；加密私钥密码错误或缺失即拒绝；`validate_client_pair()` 以 public key 比对，证书与私钥不匹配立即报 `certificate_key_mismatch`。证书正文与私钥内容都不进入 metadata。
- **Credential Service**：`qaneris/connections/credential_service.py`。负责校验、归一化与受引用保护的删除：`Catalog.count_managed_secret_references()` 会解析每个 `connection_profile_v1` 并递归统计 Secret Reference，仍被引用的 secret 返回 `managed_secret_in_use` 而不是被删除。
- **Managed 解析**：`ManagedSecretProvider` 让 `SecretReference(provider="managed", identifier="sec_...")` 可被 `SecretResolver.materialize()` 解析。provider 是按需构造的：只有真正解析 managed 引用时才要求 master key 与存储目录已配置，纯 environment/file 部署不受影响。

RS-CONN-01A 之后，Secure Datasource 生命周期已经存在：

- **Test / Create**：`test_secure_datasource()` 是纯连接测试边界，不写 Catalog、不建 initialization_job、不 scan、不写 Neo4j；`create_secure_datasource()` 固定为 `test → save`，返回 `status=created`。`Test → Save → Scan` 是三个独立步骤。
- **Update(candidate-first)**：固定顺序为 `resolve → 要求 connection_profile_v1 → 记录旧 profile → 测试候选连接 → 删除旧图 → 写入候选 profile → invalidate 旧 scan → 清理不再被引用的 managed secret`。候选连接测试失败时，旧 profile、旧活跃 scan、旧 Neo4j 图、旧 secret 全部保持不变；图删除失败则中止更新且不写入候选 profile。`update` 不等于 `scan`，调用方必须再显式 `scan_datasource()` 才能重新 READY。
- **Scan Invalidation**：`Catalog.invalidate_datasource_scan()` 在**单个事务**内停用 `scan_snapshot.active`、清空当前 `dataset` / `relation` / `dataset_sample` / `mapping`，并把 datasource 置回 `created`；历史 `scan_snapshot` 与 `initialization_job` 行保留，不物理删除。这保证“新连接 + 旧活跃 scan”不会被继续用于 Ask。
- **Delete(graph-first)**：`delete_datasource()` 固定顺序为 `resolve → 读取当前 secure profile → 收集 managed secret ids → 删除 Neo4j 图 → 在单事务内删除 Catalog datasource 及其当前事实 → 清理已无引用的 managed secret`。图删除失败时 Catalog 与凭据库不变；未知 id 返回 `datasource_not_found`。
- **Managed Secret 回收**：`old_secret_ids - new_secret_ids = obsolete_secret_ids`，逐个按 `count_managed_secret_references()` 判断，引用数为 0 才调用 `CredentialService.delete_secret()`。清理失败不回滚已完成的 datasource 变更，只记录 `secret_id` + 错误类型的 warning，不记录 secret 内容。
- **引用抽取**：`qaneris/connections/references.py` 提供的 `managed_secret_ids(profile)` 以结构化方式读取 `AuthenticationConfig` / `TLSConfig`，不使用 `json.dumps(profile)`、子串匹配或正则；`Catalog.count_managed_secret_references()` 复用同一 helper。

RS-CONN-01B 之后，TLS 物化与 Driver TLS 矩阵也已存在：

- **TLSMaterializer**：`qaneris/connections/tls_materializer.py`。正式边界是 context manager：`with TLSMaterializer().materialize(resolved) as parameters:`。文件型 Driver 需要的临时证书路径只在该 block 内有效，退出时（成功、连接失败、Adapter 异常、`KeyboardInterrupt` 全部路径）用 `shutil.rmtree` 清理。TLS 关闭时不创建任何临时目录。
- **运行时证书复检**：无论 secret 来自 environment / file / managed，物化时都重新走一遍 `CertificateValidator`；证书可能在托管上传后过期，environment/file 引用的内容也可能被改动，因此连接前必须 fail closed。client certificate + private key 同时存在时在 Driver 打开连接前调用 `validate_client_pair()`。
- **临时目录安全**：`tempfile.mkdtemp(prefix="qaneris-tls-")`，目录 `0700`、文件 `0600`；文件用 `os.open(O_CREAT | O_EXCL | O_WRONLY, 0o600)` 写入并 `flush` + `fsync`，不使用 `Path.write_text()`。固定文件名（`ca.pem` / `client-cert.pem` / `client-key.pem` / `client-combined.pem`）因父目录随机私有而安全。
- **Driver TLS 矩阵**：`qaneris/connections/tls_matrix.py` 的 `TLS_DRIVER_MATRIX` 是唯一事实来源，逐 Driver 声明 `strategy` / `custom_ca` / `mtls` / `server_name_override` / `tls13_control`，覆盖 16 个路演 Driver。Adapter 不再自行声明能力。请求 Matrix 不支持的能力（如 SQL Server 自定义 CA、Qdrant mTLS、无 1.3 能力 Driver 的 1.3 下限）抛 `tls_feature_unsupported`，绝不静默忽略、绝不自动 `verify_server=False`。
- **边界收缩**：`to_adapter_connection()` 不再输出 `connection.tls_material`；CA / client cert / private key 的 PEM 正文不进入 Adapter connection dict，也不进入 URL。TLS secret → Driver 参数的转换只由 `TLSMaterializer` 完成。
- **连接生命周期**：`ConnectionProvider.open()` / `open_profile()` 为正式 runtime API；`DatabaseInitializer`、`GroundedQueryExecutor`、显式 SQL 路径与候选连接测试均已迁移。兼容用的 `get()` 对 legacy 与 TLS-disabled secure datasource 保持可用，对 TLS-enabled datasource 抛 `tls_feature_unsupported` 并提示改用 `open()`，绝不返回已失去生命周期的 TLS 临时路径。
- **无全局状态**：`TLSMaterializer` 不设置 `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` / `GRPC_DEFAULT_SSL_ROOTS_FILE_PATH`；所有 TLS 状态都是 datasource-scoped。

但这 **不等于路演 TLS 验收已经完成**。真实服务器部署与 14/16 TLS 握手验收（RS-TLS-01 / RS-DB-02）明确延后到数据库部署到服务器后统一执行；当前优先完成 HTTP/CLI/Web 产品上传与产品入口（RS-CRED-01B 等）。MCP 不作为原始 secret 上传通道。代码支持 16 Driver ≠ 真实部署已验收；当前不宣布任何 TLS exemption。

另外，当前 FastAPI `POST /api/datasources` 和 MCP `register_datasource` 仍使用 legacy `DatasourceCreate` 路径；这些入口暂时保留兼容，但新的 Roadshow CLI/MCP/Web 不再以它们作为正式连接入口。

## 2. Roadshow Security Invariant(路演安全不变量)

密码、Token、API Key、CA Certificate(CA证书)、Client Certificate(客户端证书)、Client Private Key(客户端私钥)原文不得进入：

- Catalog 普通连接 JSON；
- Neo4j Enterprise Data Graph；
- Prompt / 模型请求；
- QueryTrace / Streaming Event；
- 普通日志、验收报告或前端回显。

Catalog 只保存不可反推出 secret 的 `SecretReference`。

### 2.1 Roadshow Deployment Boundary(路演部署边界)

因为本版本不实现应用层登录，Web/API 不得直接作为开放公网服务。路演环境必须使用受控网络、VPN 或反向代理访问控制；这属于部署边界，不在 Qaneris 内新增临时认证系统。

## 3. Roadshow Credential / Certificate Lifecycle(路演凭据与证书生命周期)

目标链路：

```text
Password / token / certificate material enters through a dedicated product boundary
→ Server-side validation
→ ManagedCredentialStore
→ return opaque secret_id
→ ConnectionProfile stores SecretReference(managed, secret_id)
→ connection test materializes secrets only at runtime
→ TLS handshake
→ Scan / Query
```

RS-CRED-01A 已将该设计落地：`managed` Secret Provider + 仓库外 `ManagedCredentialStore` + AES-256-GCM + 环境注入 Master Key。后续产品入口只能通过 `CredentialService` 创建受管 secret，并拿到 opaque `secret_id` / `SecretReference`；不得创建第二套 secret 存储或把原文写入 Catalog。后续生产可以把同一 Store Port 换成 Vault / Cloud Secret Manager，而不改变 `ConnectionProfile`。

RS-CRED-01B 已把该链路暴露为 HTTP 产品入口（`qaneris/interfaces/api/credentials.py`），并固定三条上传语义：

- **文本与证书分开入口**：JSON `POST /api/credentials` 只接受 password / token / api_key / client_private_key_password；CA 证书走 `POST /api/certificates/ca`，mTLS client identity 走 `POST /api/certificates/client-identity`。这样每类 material 的数据类型与大小限制明确，client cert/key 可以在产品入口完成 pair validation。
- **`CredentialService.create_secret()` 接受 `str | bytes`**：证书与私钥既支持 PEM 也支持 DER（DER 是二进制，不能先强制 UTF-8 decode）；文本 kind 传 bytes 直接 `invalid_request`，不做隐式编码猜测。
- **`create_client_identity()` 固定顺序**：normalize cert → normalize key（可用 transient `private_key_password` 解密）→ `validate_client_pair` → store cert → store key → 返回 `(client_certificate, client_private_key)`。pair 校验一定在存储 **之前**；两次写入 all-or-nothing，第二个 store 失败会删除刚写入的第一个 secret，不留 orphan credential。上传的私钥统一归一化为未加密 PKCS#8，因此解密密码只在上传请求内存在，不会自动创建 `CLIENT_PRIVATE_KEY_PASSWORD` secret。

产品入口只投影 `ManagedCredentialPublic`（`secret_id` 而非内部 `ManagedSecretInfo.id`），只调用 `QanerisService` 公开方法；证书/私钥上传不创建 datasource、不测试连接、不写 Neo4j。CLI / MCP / Web 复用同一批 API，不各自实现证书解析、加密或 secret 存储。

## 4. Certificate Validation(证书验证)

上传阶段至少验证：

- PEM/DER 是否可解析，文件类型与声明用途一致。
- CA / Client Certificate 是否在有效期内。
- Client Certificate 与 Client Private Key 是否匹配。
- 加密私钥需要密码时，密码能否正确解锁。
- 单文件大小、总上传大小、允许扩展名和内容格式有上限。

真实连接阶段必须继续验证：

- TLS handshake 成功。
- `verify_server=true` 时服务器证书链可由指定 CA 验证。
- `server_name` / hostname 校验符合 Driver 能力。
- TLS 最低版本满足 `minimum_version`，Roadshow 默认不低于 TLS 1.2。
- 需要 mTLS 的数据库必须证明客户端证书认证成功。

单纯“证书能解析”不能替代真实数据库 TLS connection test。

## 5. TLS Materialization(TLS材料物化)

不同 Driver 对证书入参形式不同：有的接受 PEM 内容，有的只接受文件路径。因此 Roadshow 需要统一 `TLSMaterializer` 边界：

```text
SecretReference
→ ManagedCredentialStore resolve
→ memory or private temporary directory
→ driver-specific ca/cert/key arguments
→ connection closes
→ temporary material deleted
```

临时文件不得使用可预测公共路径；权限应限制为服务账号可读。异常路径同样必须清理。

## 6. Rotation / Delete(轮换与删除)

Secret 保持 immutable(不可原地修改)。正式轮换固定为：

```text
create new secret
→ build candidate ConnectionProfile
→ resolve SecretReference
→ real connection test
→ success: switch datasource profile
→ delete old secret only when reference_count == 0
```

connection test 失败时旧 Datasource 与旧 SecretReference 完全不变。仍被任何 Datasource 引用的 secret 禁止删除；Datasource 删除或轮换后，只有无引用的 managed secret 才能物理清理。

RS-CONN-01A 额外固定两点：轮换只会回收“旧 profile 有、新 profile 没有”的 secret —— 候选 profile 独有的 secret 属于调用方，即使候选测试失败也不得销毁；清理阶段的失败不会回滚已持久化的 datasource 变更，只留下 warning 供后续清理。

legacy（非 `connection_profile_v1`）datasource 不自动迁移：`update_secure_datasource()` 对它会返回 `datasource_secure_profile_required`（409），而 `delete_datasource()` 允许删除它，但不会尝试对其 legacy 连接 JSON 做 managed secret 回收。

## 7. Secure Datasource Application Contract(安全数据源应用契约)

RS-CONN-01A 的正式 Application Service(应用服务)入口固定为：

```text
test_secure_datasource(SecureDatasourceTest) -> SecureDatasourceTestResult
create_secure_datasource(SecureDatasourceCreate) -> Datasource   # status=created，不 scan
update_secure_datasource(datasource_id, SecureDatasourceUpdate) -> Datasource
delete_datasource(datasource_id) -> None
list_datasources(workspace_id) -> list[Datasource]
inspect_datasource(datasource_id) -> DatasourceDetail
scan_datasource(datasource_id) -> list[DatasetInfo]              # 唯一扫描入口
```

create/update 必须采用 candidate-first(候选先测试)：先解析 SecretReference、物化候选连接并执行真实 connection test；只有成功后才写入/切换 Catalog。update 失败不得破坏旧配置。Datasource 删除后，只能清理已经无引用的 managed secrets。

结果与错误契约：`SecureDatasourceTestResult` 只有 `ok` / `driver` / `tls_enabled`，不含 host password、token、API Key、证书正文、私钥、materialize 后的连接字典或 Adapter kwargs。新增错误码为 `datasource_connection_test_failed`、`datasource_secure_profile_required`(409)、`datasource_update_failed`、`datasource_delete_failed`；所有 Adapter / Driver 异常在越过产品边界前都经过 `safe_error()` / `sensitive_values()`，绝不返回驱动原始异常。

Legacy `DatasourceCreate / connection_json` 暂时保留兼容，但标记为非 Roadshow 正式入口，新的 CLI / MCP / Web 不继续扩展该路径。

## 8. TLS Materialization & Driver Matrix(TLS材料物化与驱动矩阵)

RS-CONN-01B 建立统一 `TLSMaterializer` 边界：

```text
SecretReference
→ SecretResolver / ManagedCredentialStore
→ TLSMaterializer
├─ driver accepts content → memory
└─ driver requires paths → private temporary directory
→ driver-specific connection arguments
→ connection closes
→ cleanup on success and failure
```

临时目录和证书/私钥文件不得使用可预测公共路径，权限仅允许服务账号访问。每个 Driver 必须在显式 TLS Matrix 中记录 CA、client certificate、private key、hostname verification、minimum TLS、mTLS、memory/path materialization 参数；不得依赖通用字典猜测 Driver 行为。

## 9. Product Surface Boundary(产品入口边界)

- CLI 继续本地调用 `QanerisService`，不改造成 HTTP Client；password/token 使用隐藏输入或 stdin，certificate/private key 通过文件路径导入，禁止 secret 出现在 argv/history。
- MCP 不接收任何 secret 原文，只使用 `SecretReference`、`datasource_id` 等非敏感引用。
- Web 使用 Driver → Endpoint → Authentication → TLS/Certificate → Test Connection → Save → Scan → Ready 的固定向导；上传成功后前端只持有 `secret_id` / `SecretReference`。
- API/CLI/Web 的上传边界统一调用现有 `CredentialService`；不得各自实现证书解析、加密或 secret 存储。

## 10. 16-DB / 14-TLS Server Acceptance(16库与14个TLS服务器验收)

`NLQuery-Test-Dataset` 当前提供 16 个服务型数据库测试目标，另有容器化 SQLite。16 个服务型数据库的真实 TLS / connection / scan / Neo4j 回读**不再作为当前 Product Surface 开发前置条件**，统一延后到数据库部署到服务器后执行。

服务器阶段目标仍保持：16 个服务型数据库完成 Qaneris 真实连接与 scan，其中 14 个完成真实 SSL/TLS handshake + connection + scan 验收。本文 **不提前指定哪两个数据库豁免 TLS**；豁免只能依据服务器上的实际镜像、协议与 Driver 支持结果确定并记录原因。

最终服务器验收矩阵至少记录：driver、endpoint、auth method、TLS on/off、CA、client cert、hostname verify、minimum TLS、connection test、scan、Neo4j validation、notes。代码级 TLS Matrix DONE 不能代替这份服务器验收证据。

## 11. Definition of Done(完成定义)

- API/CLI/Web 可以安全创建/上传凭据与证书并得到 opaque SecretReference；MCP 只消费引用，不接收 secret 原文。
- Catalog / API / CLI / MCP / logs / trace 中没有 secret 原文。
- Secure Datasource create/update/test/delete 采用 candidate-first 失败安全行为，更新失败时旧配置保持可用。
- 在服务器部署阶段，14 个路演目标完成真实 TLS connection test + Qaneris scan；该项是最终 Release Gate，不阻塞当前 CRED-01B / CLI / MCP / Skill / Web 开发。
- 证书格式错误、过期、key mismatch、错误 CA、错误 hostname、错误私钥密码均失败关闭。
- Driver 需要文件路径时使用受控临时 materialization，并验证清理。
- Rotation / delete 有自动化测试。
- Driver 需要文件路径时使用受控临时 materialization，并在成功/失败路径都验证清理。
- Roadshow 新 CLI / MCP / Web 不再依赖 legacy `DatasourceCreate / connection_json`。
