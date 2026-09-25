# SmartData 智能问数平台

[English](README.md) | [简体中文](README.zh-CN.md)

SmartData 是面向企业数据的只读、可治理、可追溯问数系统。自然语言请求统一进入 `SmartDataService.ask()`；业务问数依次经过意图解析、语义检索、数据绑定、查询计划、校验、只读执行，返回类型化结果及证据。结构探索问题读取已发布的扫描结构，再由配置好的模型生成受结构约束的概述。Web、CLI、API 和 MCP 共用服务入口。

## 当前能力与边界

- Web 工作区提供对话式智能问数、数据源管理、Excel 导入。Analyze 使用 Conversation → Run → 可续订 SSE → 每轮独立的结果、图表和执行依据；历史对话可重新打开。旧 `POST /api/ask` 与 `POST /api/ask/stream` 仍供兼容客户端、CLI、MCP 和 Skill 使用。
- 在问数页选中已扫描的数据源后，可以问“这个数据库有哪些表？”“这个表有什么数据？”或“MySQL 数据库里面是什么数据？”。这类结构探索问题由后端读取该数据源已发布到 Neo4j 的表和字段，返回结构清单；配置了模型时，还会基于有限的表名和字段名生成概述。模型不可用时仍返回扫描结构。选中的数据源含多张表时，“这个表”不会被猜作其中某张表；页面会列出结构并提示写明表名。这里不读取表内记录；问题点名的数据库类型与当前选中数据源不一致时，页面会提示先切换数据源。
- 数据源页面支持选择驱动、填写连接信息与 SSL/证书设置、测试连接、保存与扫描、重新连接和删除。连接由后端发起；浏览器不直接连接数据库。
- 后端提供只读查询、数据源生命周期、凭据及证书上传接口。CLI 已包含 `smartdata ask`、数据源、凭据、证书和验收命令；MCP 提供 `ask_data` 等工具。
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

服务以脱离终端的子进程启动，`setup.py` 退出后继续运行，日志在 `.tools/logs/`。停止：`pkill -f 'uvicorn smartdata' ; pkill -f 'vite --host'`。

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
.venv/bin/python -m pip install -e '.[sql,neo4j]'
./start-web.sh
```

脚本读取仓库根目录的 `.env`（如果存在），在 `127.0.0.1:8000` 启动 FastAPI、在 `127.0.0.1:5173` 启动 Vite，并打开浏览器。也可以运行 `./start-web.sh --no-open`。按 Ctrl+C 停止脚本启动的进程；如果端口已有服务，脚本会复用。使用其他驱动时，还需安装 `pyproject.toml` 中相应的可选依赖。

页面可打开不代表问数链路已就绪。自然语言问数需要可用的模型和 Neo4j；扫描和 Excel 导入也依赖对应的运行环境。可以用 `smartdata doctor` 检查配置。

## 添加数据库与证书连接

在「数据源」中选择数据库驱动，然后在「基本设置」填写连接名称、工作区、部署方式、主机与端口（或 URL）、数据库和认证信息；在「SSL / 证书」中启用加密连接，按服务端要求上传 CA 证书，双向 TLS 再上传客户端证书与私钥。「高级选项」用于驱动额外参数。点击「测试连接」，成功后才能「保存并扫描」；扫描失败时，已保存的数据源仍可见，可重试扫描。

云端 PostgreSQL 使用云服务商给出的 **数据库连接端口**，通常为 `5432`，以实际配置为准。若连接本机 Docker 映射端口 `0.0.0.0:5433->5432/tcp`，SmartData 后端运行在宿主机时填写 `5433`；后端与数据库同在 Docker 网络时通常填写容器端口 `5432` 和可解析的容器主机名。TLS 证书中的服务器名称须与连接目标匹配。详细协议与驱动能力见 [连接模型](docs/connection-model.md)。

Web 新建受管密码或上传证书前，必须给**后端进程**配置以下环境变量，并重启后端：

```dotenv
SMARTDATA_SECRET_STORE_DIR=/path/outside/SmartData/managed-secrets
SMARTDATA_MASTER_KEY=<32 字节随机密钥的 URL-safe Base64 编码>
```

存储目录必须在项目仓库之外；主密钥必须稳定保存，不能每次启动重新生成。缺少配置时会返回 `credential_store_configuration_error`。不要把真实主密钥、数据库密码或证书私钥提交到 Git。受管密码、证书与私钥由后端加密存储，连接配置仅引用其 Secret ID。底层安全配置还支持 `environment` 和 `file` 类型的 `SecretReference`。

数据源记录和连接配置保存在 `SMARTDATA_CATALOG` 指定的 SQLite catalog 中，默认是后端工作目录的 `smartdata.db`；Excel 与扫描产物写入 `SMARTDATA_ARTIFACT_ROOT`，默认位于项目同级的 `SmartDataArtifacts`；受管凭据单独写入 `SMARTDATA_SECRET_STORE_DIR`。因此清理浏览器缓存不会删除已保存的数据源。

## CLI、API 与 MCP

```bash
smartdata doctor
smartdata ask --help
smartdata source --help
smartdata credential --help
smartdata certificate --help
smartdata acceptance phase1
smartdata-api
smartdata-mcp
```

运行 API 后可访问 `http://127.0.0.1:8000/docs`。主要入口包括 `POST /api/ask`、`POST /api/ask/stream` 和数据源、凭据、证书接口。兼容命令 `smartdata-scan` 仍保留。项目 Skill 位于 [skill/SKILL.md](skill/SKILL.md)。

## 本地 Neo4j

```bash
export SMARTDATA_NEO4J_PASSWORD='choose-a-local-password'
docker compose up --build
```

后端在 Compose 外运行时，按实际环境设置 `SMARTDATA_NEO4J_URI`、`SMARTDATA_NEO4J_USERNAME`、`SMARTDATA_NEO4J_PASSWORD`，必要时设置 `SMARTDATA_NEO4J_DATABASE`。

## 文档与开发

文档索引见 [docs/README.md](docs/README.md)；产品入口见 [产品界面说明](docs/product-interfaces.md)；实现进度与未完成的真实环境验收见 [开发路线图](docs/development-roadmap.md)。

```bash
pytest
ruff check .
```

所有产品入口都必须遵守数据绑定、校验、修订边界和只读执行约束。
