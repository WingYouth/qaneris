# Qaneris 智能问数平台

[English](README.md) | [简体中文](README.zh-CN.md)

Qaneris 是面向企业数据的只读、可治理、可追溯问数系统。自然语言请求统一进入 `QanerisService.ask()`；业务问数依次经过意图解析、语义检索、数据绑定、查询计划、校验、只读执行，返回类型化结果及证据。结构探索问题读取已发布的扫描结构，再由配置好的模型生成受结构约束的概述。Web、CLI、API 和 MCP 共用服务入口。

## 当前能力与边界

- Web 工作区提供对话式智能问数、数据源管理、Excel 导入。Analyze 使用 Conversation → Run → 可续订 SSE → 每轮独立的结果、图表和执行依据；历史对话可重新打开。旧 `POST /api/ask` 与 `POST /api/ask/stream` 仍供兼容客户端、CLI、MCP 和 Skill 使用。
- 在问数页选中已扫描的数据源后，可以问“这个数据库有哪些表？”“这个表有什么数据？”或“MySQL 数据库里面是什么数据？”。这类结构探索问题由后端读取该数据源已发布到 Neo4j 的表和字段，返回结构清单；配置了模型时，还会基于有限的表名和字段名生成概述。模型不可用时仍返回扫描结构。选中的数据源含多张表时，“这个表”不会被猜作其中某张表；页面会列出结构并提示写明表名。这里不读取表内记录；问题点名的数据库类型与当前选中数据源不一致时，页面会提示先切换数据源。
- 数据源页面支持选择驱动、填写连接信息与 SSL/证书设置、测试连接、保存与扫描、重新连接和删除。连接由后端发起；浏览器不直接连接数据库。
- 后端提供只读查询、数据源生命周期、凭据及证书上传接口。CLI 已包含 `qaneris ask`、数据源、凭据、证书和验收命令；MCP 提供 `ask_data` 等工具。
- Adapter Registry 注册了 21 个驱动。注册数量不代表每个驱动都已经在真实服务器上通过连接、TLS 和查询验收。
- Phase 1 历史固定问题集曾在真实 LLM + Neo4j + SQLite 环境中得到 18/18 预期结果；它不代表系统总体准确率。可视化链路的本地真实查询与浏览器验收记录见 [RS-VIZ-02 验收记录](docs/rs-viz-02-acceptance.md)，其中也记录了外部模型额度和 Docker Hub 拉取镜像的限制。

独立仓库 `JingJIang96200/NLQuery-Test-Dataset` 提供 16 个服务型数据库及容器化 SQLite 的测试数据和 Docker 定义。测试数据齐备不等于这些服务已部署到目标环境，也不等于全部驱动完成真实验收。联合分析支持受治理的合并操作和已确认的跨数据源关联键。

## 一次运行完成环境准备

首次拿到仓库时运行一次即可把环境配好：

```bash
python3 setup.py
```

它会按顺序检查并**自动修复**：缺失的 `uv`、Node 与 Docker，缺失的 `.env` 及其本机密钥，Python 与前端依赖，Neo4j 容器，以及后端与前端服务。其中会自动生成的是本机密钥（Neo4j 容器密码、凭据库主密钥），写入 `.env` 时权限为 600。

只有一项无法代填：**模型端点凭据**。它需要真实可用的 API Key，脚本会在交互式终端里询问你；没有它时问数会返回 `intent_parsing_failed`，但扫描、结构查看与 Web 工作区都照常可用。

每项结果只有三种状态：`可用`、`已修复`、`待处理`。退出码 `0` 表示没有待处理项，`1` 表示有，`2` 表示参数错误。重复运行是幂等的：已经就绪的东西不会再被改动。

服务以脱离终端的子进程启动，`setup.py` 退出后继续运行，日志在 `.tools/logs/`。停止：`pkill -f 'uvicorn qaneris' ; pkill -f 'vite --host'`。

常用参数：

```bash
python3 setup.py --check          # 只报告，不做任何修改
python3 setup.py --json           # 机器可读结果
python3 setup.py --no-start       # 只准备环境，不启动服务
python3 setup.py --no-prompt      # 不询问模型凭据
python3 setup.py --no-install-docker   # 不自动安装 Docker
```

自动安装 Docker 时，macOS 从官方地址下载 Docker Desktop（约 586 MB，支持断点续传，写入 `/Applications`），Linux 运行 Docker 官方脚本（需要 root，必须再加 `--allow-root`）。已安装但守护进程未运行时只提示 `open -a Docker`，不代为启动 GUI。

若 Neo4j 数据卷已存在，其密码在首次初始化时就已固化。脚本检测到端口无服务但数据卷存在时会提示，不会擅自删除你的图数据。

## 启动 Web 工作区

在仓库根目录准备 Python 3.11+ 虚拟环境和项目依赖，并安装 Node.js/npm。`start-web.sh` 使用根目录的 `.venv/bin/python`；首次运行若缺少前端 `node_modules`，会执行 `npm ci`。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[sql,redis,mongodb,cassandra,hbase,neo4j,influxdb,search,milvus,qdrant]'
./start-web.sh
```

脚本读取仓库根目录的 `.env`（如果存在），在 `127.0.0.1:8000` 启动 FastAPI、在 `127.0.0.1:5173` 启动 Vite，并打开浏览器。也可以运行 `./start-web.sh --no-open`。按 Ctrl+C 停止脚本启动的进程；如果端口已有服务，脚本会复用。上述安装命令已包含页面支持的数据库驱动；在本机连接 SQL Server 还需要系统安装 ODBC Driver 18 for SQL Server。

页面可打开不代表问数链路已就绪。自然语言问数需要可用的模型和 Neo4j；扫描和 Excel 导入也依赖对应的运行环境。可以用 `qaneris doctor` 检查配置。

## 添加数据库与证书连接

在「数据源」中选择数据库驱动，然后在「基本设置」填写连接名称、工作区、部署方式、主机与端口（或 URL）、数据库和认证信息；在「SSL / 证书」中启用加密连接，按服务端要求上传 CA 证书，双向 TLS 再上传客户端证书与私钥。「高级选项」用于驱动额外参数。点击「测试连接」，成功后才能「保存并扫描」；扫描失败时，已保存的数据源仍可见，可重试扫描。

云端 PostgreSQL 使用云服务商给出的 **数据库连接端口**，通常为 `5432`，以实际配置为准。若连接本机 Docker 映射端口 `0.0.0.0:5433->5432/tcp`，Qaneris 后端运行在宿主机时填写 `5433`；后端与数据库同在 Docker 网络时通常填写容器端口 `5432` 和可解析的容器主机名。TLS 证书中的服务器名称须与连接目标匹配。详细协议与驱动能力见 [连接模型](docs/connection-model.md)。

Web 新建受管密码或上传证书前，必须给**后端进程**配置以下环境变量，并重启后端：

```dotenv
QANERIS_SECRET_STORE_DIR=/path/outside/Qaneris/managed-secrets
QANERIS_MASTER_KEY=<32 字节随机密钥的 URL-safe Base64 编码>
```

存储目录必须在项目仓库之外；主密钥必须稳定保存，不能每次启动重新生成。缺少配置时会返回 `credential_store_configuration_error`。不要把真实主密钥、数据库密码或证书私钥提交到 Git。受管密码、证书与私钥由后端加密存储，连接配置仅引用其 Secret ID。底层安全配置还支持 `environment` 和 `file` 类型的 `SecretReference`。

数据源记录和连接配置保存在 `QANERIS_CATALOG` 指定的 SQLite catalog 中，默认是后端工作目录的 `qaneris.db`；Excel 与扫描产物写入 `QANERIS_ARTIFACT_ROOT`，默认位于项目同级的 `QanerisArtifacts`；受管凭据单独写入 `QANERIS_SECRET_STORE_DIR`。因此清理浏览器缓存不会删除已保存的数据源。

## CLI、API 与 MCP

CLI 是本地编排层：每条命令只调用 `QanerisService` 的公开方法，不改成 HTTP 客户端，也不直接访问 Catalog、凭据库、证书校验器、Adapter 或 Neo4j。因此 CLI、API、MCP、Skill 与 Web 共用同一套产品规则。

### 安装与入口

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[sql,neo4j]'   # 核心命令
.venv/bin/python -m pip install -e '.[shell]'       # 可选：交互式会话
```

四个入口：`qaneris`（主命令）、`qaneris-shell`（直接进交互式会话）、`qaneris-api`、`qaneris-mcp`。

命令按当前目录解析 `.env`（也可用全局 `--env-file FILE`、环境变量 `QANERIS_ENV_FILE` 指定），数据源记录存于 `QANERIS_CATALOG`（默认当前目录的 `qaneris.db`）。**因此请固定在同一目录下运行**，否则会读到另一个 catalog。

### 交互式会话

```bash
qaneris shell
```

inline TUI：横幅与常驻状态行由终端 UI 绘制，命令输出留在终端原生 scrollback（可正常上滚、复制、搜索）。每行交给与一次性命令完全相同的入口，所以输出、退出码、错误规则逐字一致。

- 内建词 `exit` / `quit` / `help` / `clear`（`?` 为 `help` 别名），仅在不是已注册命令时生效，不会遮蔽命令树。
- 每行会剥掉行首重复的 `qaneris`，因此从 README 复制的 `qaneris source list` 可直接用；只输入程序名时打印帮助。会话内输入 `shell` 打印 `shell` 的帮助，不会嵌套第二层会话。
- 单条命令的退出码显示在状态行，**不作为会话退出码**；会话正常结束返回 0。
- Ctrl-C 取消当前输入行而不退出，Ctrl-D 退出。
- 非终端 stdin 一律拒绝（退出码 2），避免脚本隐式进入交互模式。
- 横幅随终端宽度自适应（放得下时两栏并排，放不下时纵向堆叠）；状态行按语义给退出码上色：`0` 绿、`1` / `2` 红。
- 命令历史写入 `.tools/shell_history`（目录 0700 / 文件 0600，已被 Git 忽略）；`--no-history` 关闭，`--history FILE` 改路径。

```bash
qaneris shell --no-banner        # 跳过欢迎面板
qaneris shell --no-history       # 不读写历史
qaneris shell --history ~/.qaneris_history
```

### 命令一览

```text
qaneris doctor [--json]
qaneris ask QUESTION [--workspace ID] [--datasource ID] [--max-rows N] [--stream] [--json]

qaneris source list [--workspace ID] [--json]
qaneris source show DATASOURCE_ID [--json]
qaneris source scan-one DATASOURCE_ID [--json]
qaneris source test-one --config FILE [--json]
qaneris source create --config FILE [--json]
qaneris source update DATASOURCE_ID --config FILE [--json]
qaneris source delete DATASOURCE_ID [--json]
qaneris source test --config FILE [--json]
qaneris source scan --config FILE [--json]
qaneris source import-excel FILE [--name NAME] [--workspace ID] [--json]

qaneris credential add --kind {password|token|api_key|client_private_key_password} [--stdin] [--json]
qaneris credential show SECRET_ID [--json]
qaneris credential delete SECRET_ID [--json]

qaneris certificate add-ca FILE [--json]
qaneris certificate add-client --certificate FILE --private-key FILE
                              [--private-key-password-prompt | --private-key-password-stdin] [--json]
qaneris certificate inspect SECRET_ID [--json]
qaneris certificate delete SECRET_ID [--json]

qaneris acceptance phase1 [--question-class {A,B,C,D,E,F}] [--repeat N] [--no-trace] [--json]
qaneris acceptance ask --suite phase1 [...同上]

qaneris shell [--history FILE | --no-history] [--no-banner]
```

### 环境自检与验收

`qaneris doctor` 检查 artifact root、模型配置、Neo4j 配置与连通性、catalog 配置，全部只报告可发布状态，不泄露凭据内容。

`qaneris acceptance phase1` 跑固定六类问题集（`--question-class A-F` 单选、`--repeat N` 重复、`--no-trace` 关闭轨迹）。它是**历史基线的复现工具**：仓库记录的 18/18 是真实 LLM + Neo4j + SQLite 环境下的固定问题集结果，不代表系统总体准确率。

### 问数

`qaneris ask` 是非交互式命令：one command → one `AskRequest` → one `AskResponse`。`--datasource` 只接受数据源 id（不猜名称）；`--max-rows` 的合法范围由 `AskRequest` 契约校验。

| 命令 | stdout | 退出码 |
| --- | --- | --- |
| `ask "Q"` | 人类可读：Status / Datasource / Scan version / Rows / Truncated / 结果表格 / 安全查询展示 / 简要 Plan。`clarification_required` 输出澄清问题；失败只输出稳定错误码，无 traceback | 0 / 1 |
| `ask "Q" --json` | 恰好一个 JSON 文档，是 `AskResponse` 的稳定字段投影 | 0 / 1 |
| `ask "Q" --stream` | 每个真实 `AskEvent` 一行 `[stage] headline (事实...)`；不伪造阶段、不显示模型私有推理、不显示 secret | 0 / 1 |
| `ask "Q" --stream --json` | 严格 JSONL，每行是该事件自身的序列化结果，`done` 恒为最后一行 | 0 / 1 |

澄清（`clarification_required`）是正常产品结论，退出码为 0，与 API / MCP 一致。

```bash
qaneris ask "总实收销售额是多少？" --datasource ds_c996e9437e6a
qaneris ask "这个数据库有哪些表？" --datasource ds_c996e9437e6a
qaneris ask "上个月的订单量是多少？" --stream
```

### 数据源生命周期

推荐用安全配置（`SecureDatasourceCreate`）走 `Test → Save → Scan`：

```bash
qaneris source test-one --config datasource.json     # 只测连接，不保存、不扫描、不写图
qaneris source create --config datasource.json       # 测试并保存，停在 created，不自动扫描
qaneris source scan-one ds_c996e9437e6a              # 显式扫描并发布结构
```

`create` 与 `update` 都**不会自动扫描**，创建后需显式 `scan-one`。`source list` / `show` / `scan-one` 只发布公开字段（ID / Name / Kind / Driver / Status / Workspace 及扫描状态），不含连接文档、密钥引用或凭据。

配置文件格式与批量 `source test` / `scan` 完全相同，必须是单个数据源（多个会被拒绝而非静默挑选）：

```json
{
  "name": "retail-mongodb",
  "kind": "document",
  "workspace_id": "default",
  "connection_profile": {
    "driver": "mongodb",
    "deployment_mode": "local",
    "endpoint": { "url": "mongodb://127.0.0.1:27018", "database": "retail" },
    "authentication": { "method": "none" }
  }
}
```

密码用密钥引用而非原文，`provider` 支持 `managed` / `environment` / `file`：

```json
"authentication": {
  "method": "password",
  "username": "readonly",
  "password": { "provider": "environment", "identifier": "QANERIS_PG_PASSWORD" }
}
```

**配置文件中的内联密钥会被拒绝**（`password` / `token` 原文、URL 内嵌密码一律报错），必须改用 `SecretReference`。更多示例见 `configs/acceptance/local/`。

### 凭据与证书

创建受管凭据前，必须给**进程**配置存储目录与主密钥：

```dotenv
QANERIS_SECRET_STORE_DIR=/path/outside/Qaneris/managed-secrets
QANERIS_MASTER_KEY=<32 字节随机密钥的 URL-safe Base64 编码>
```

存储目录必须在仓库之外；主密钥必须稳定保存，不能每次启动重新生成。缺少配置时返回 `credential_store_configuration_error`。

**Secret 原文永不进入 argv**：命令树里不存在 `--password VALUE`、`--token VALUE`、`--api-key VALUE`、`--secret VALUE`、`--value VALUE`、`--private-key-password VALUE`。只有两条输入通道：

```bash
qaneris credential add --kind password                      # 隐藏交互输入（getpass，不回显）
printf '%s' "$PG_PASSWORD" | qaneris credential add --kind password --stdin   # 显式管道
```

管道必须显式加 `--stdin`，否则会被拒绝（避免误把重定向的文件当密码读入）。`credential add --kind` 只接受文本类型；`ca_certificate` / `client_certificate` / `client_private_key` 必须走 `certificate` 命令，不能借 `--kind` 绕过文件边界。

### Excel 导入

```bash
qaneris source import-excel orders.xlsx --name "Orders 2026" --workspace default
```

Excel 是**导入源**，不是第二套查询引擎：工作簿先被确定性校验并物化为不可变 SQLite，之后走同一套 Scan → Graph → Grounding → Query 链路。退出码 0 只在导入 READY（含 Neo4j 发布校验通过）时可达。

### 退出码与输出约定

| 退出码 | 含义 |
| --- | --- |
| `0` | 命令完成（含 `clarification_required`，它是产品结论） |
| `1` | 命令执行了但失败（连接失败、产品错误、运行时异常） |
| `2` | 用法或配置错误（参数不合法、配置文件无法解析） |

- `--json` 时 stdout 只允许**单一 JSON 文档**；驱动与 provider 的杂散输出被重定向到 stderr，因此 JSON 纯净性不依赖驱动是否安静。
- `--stream --json` 时 stdout 只承载事件，`done` 恒为最后一行。
- 未预期的异常只暴露类型，不显示任意消息（避免消息里带出凭据）。

### API 与 MCP

```bash
qaneris-api     # 运行后访问 http://127.0.0.1:8000/docs
qaneris-mcp
```

主要接口包括 `POST /api/ask`、`POST /api/ask/stream` 和数据源、凭据、证书接口。兼容命令 `qaneris-scan` 仍保留。项目 Skill 位于 [skill/SKILL.md](skill/SKILL.md)。

## 本地 Neo4j

```bash
export QANERIS_NEO4J_PASSWORD='choose-a-local-password'
docker compose up --build
```

后端在 Compose 外运行时，按实际环境设置 `QANERIS_NEO4J_URI`、`QANERIS_NEO4J_USERNAME`、`QANERIS_NEO4J_PASSWORD`，必要时设置 `QANERIS_NEO4J_DATABASE`。

## 文档与开发

文档索引见 [docs/README.md](docs/README.md)；产品入口见 [产品界面说明](docs/product-interfaces.md)；实现进度与未完成的真实环境验收见 [开发路线图](docs/development-roadmap.md)。

```bash
pytest
ruff check .
```

所有产品入口都必须遵守数据绑定、校验、修订边界和只读执行约束。
