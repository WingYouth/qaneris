# RS-VIZ-02 可视化验收记录

日期：2026-09-24。

## 结论

RS-VIZ-01 的确定性可视化链路已在真实 Neo4j、真实 SQLite 执行、HTTP/SSE 和 Chrome 页面中验收：固定合成 Excel 上传、图发布、治理绑定、Ask 查询、ChartSpec、SVG 渲染共 **57/57 检查通过**。柱状图与饼图使用同一返回结果；折线图使用后端计划的 `time_field`，保持返回行顺序。标量与空结果回到表格；无效/不支持的 ChartSpec 被拒绝并安全降级。

本次使用固定的本地意图模型，仅将已知测试问题转为 `BusinessQuery`。它不生成 SQL、图表或答案。检索、图读取、Grounding、QueryPlan、只读 SQL、SSE、前端策略和 Chrome SVG 均为正式实现。因此此结果证明图表和查询执行链路，不代表外部模型提供商可用。

## 重现

先启动本地 Neo4j：

```bash
open -a Docker
docker compose -p qaneris-viz02 up -d neo4j --pull never
```

在仓库根目录运行（`.env` 只用于本地 Neo4j 凭据，模型变量在本次本地验收中移除）：

```bash
set -a; source .env; set +a
unset QANERIS_MODEL_BASE_URL QANERIS_MODEL_API_KEY QANERIS_MODEL_NAME
QANERIS_ARTIFACT_ROOT=/private/tmp/qaneris-viz02-local \
  .venv/bin/python -m qaneris.scripts.acceptance.web_visualization
```

脚本使用固定的 `orders.xlsx` 合成数据，调用真实 HTTP/SSE 前端模块，并用 Chrome 打开生产构建页面。最终检查、每种图表的真实返回数据及饼图/折线图截图写入 `QANERIS_ARTIFACT_ROOT/acceptance/web-viz-<UTC时间>/`。2026-09-24 本地记录为 `/private/tmp/qaneris-viz02-local/acceptance/web-viz-20260924T062546Z`，`visualization-checks.json` 中 57 项均为 `true`。

## Python 失败归因与回归

此前 5 个失败用例属于旧版自然语言直查契约测试：未配置模型时在意图解析前置条件失败；加载 `.env` 后会调用外部模型，而测试仍断言旧式 SQL plan/analysis。现已将这些用例明确改为验证现存的显式只读 SQL 兼容路径，并在 API 错误用例中显式断言未配置模型的 `intent_parsing_failed`。统一 Ask 的 Grounding/Plan/Stream 行为仍由现有独立测试覆盖。

```text
Frontend: 50 passed; production build passed
Python:   1463 passed, 3 skipped
Ruff:     All checks passed
```

完整 Python 回归使用 `QANERIS_ARTIFACT_ROOT=/private/tmp/qaneris-viz02-tests .venv/bin/python -m pytest -q`，避免默认仓库外 artifact 目录的 sandbox 权限问题。

## 外部环境边界

- 外部模型真实验收：配置的 OpenRouter 服务可达，但本次 `web_workspace` 返回 HTTP 429 `free-models-per-day`，导致模型版 Ask 没有完成。该脚本如实报告 `FAIL`，不得计为通过；恢复账户额度或提供可用模型后需重跑。
- Docker：Docker Desktop daemon 已启动，版本 29.4.3；`docker build -t qaneris:rs-viz-02 .` 在拉取 `node:22-slim`、`python:3.12-slim` 的 Docker Hub 授权 token 时连接超时。两种基础镜像均未在本机缓存。本机生产前端构建通过，Python API 已在验收中启动并返回健康状态；容器镜像构建仍需 Docker Hub 可达后重跑。此次没有修改 Dockerfile。

上述两项是外部服务限制，不改变 57 项本地图表验收结论，也不能被记录成外部模型或 Docker 镜像已通过。
