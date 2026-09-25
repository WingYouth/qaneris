# SmartData Roadshow Overview(路演总览)

**SmartData source baseline：main @ d39cc71f320aad2d41db72d9d1abcd49a5b275f0**  
**Test-data baseline：JingJIang96200/NLQuery-Test-Dataset @ cb1c520dbc150412e271049cd45298a99c5d8a16**

## 1. One Sentence(一句话)

SmartData 是面向企业数据的可信 AI 问数系统：让模型理解业务问题，让受治理语义、Neo4j 企业数据图和校验链决定真实数据库事实，再以只读方式执行并返回可追溯结果。

```text
Question
→ Business Intent
→ Governed Semantics
→ Graph-confirmed Physical Facts
→ QueryPlan
→ Validation
→ Read-only Execution
→ Result + Evidence
```

## 2. What Is Already Real(已经真实完成的部分)

- `SmartDataService.ask()` 的 Phase 1 可信查询主链已完成。
- Scan → Neo4j Enterprise Data Graph 已完成；关系只接受数据库/配置确认的真实关系，不根据同名字段猜测。
- 固定真实 LLM + Neo4j + SQLite 验收集曾完成六类问题 × 3 次，共 18/18 符合预期。这个数字只代表该固定验收集，不代表系统总体准确率 100%。
- Adapter Registry 当前注册 21 个 driver；它代表代码入口，不代表 21 个真实数据库都做过问数 E2E。
- API `POST /api/ask` 与 MCP `ask_data` 已存在。
- CLI 已有 doctor、Phase 1 acceptance、source test/scan、source import-excel，以及非交互式 `smartdata ask`（human/JSON，RS-CLI-01A）。
- React/Vite Web Foundation 已存在，但还不是完整 Ask 产品。

## 3. Roadshow Release Must Add(路演前必须补齐)

Roadshow 目标不是继续堆内部模块，而是把核心引擎包装成可演示、可连接、可追踪的产品：

- Web / CLI / Skill / MCP 四种产品形态可用。
- 统一 Streaming / Progress(流式进度)契约。
- Web 展示结构化 Execution Trace、QueryPlan、SQL/Native Query Evidence、Result Table 和 Visualization。
- Excel `.xlsx` 完成 Upload → SQLite → Scan → Neo4j → Ask 全链路。
- `NLQuery-Test-Dataset` 的 16 个服务型数据库部署到路演服务器并由 SmartData 做真实 connection + scan 验收。
- 16 个服务型库中的 14 个完成 SSL/TLS 真实连接；用户证书必须有上传、验证、受管保存、运行时物化、轮换和删除能力。
- 固定 Roadshow Dataset / Questions / Acceptance Script，并录制演示视频。

## 4. Important Current Gaps(当前不能讲成已完成的部分)

- 测试数据库仓库已完成 ≠ 路演服务器已经部署。
- `TLSConfig` 已有 ≠ Web 用户证书上传与 CredentialStore 已完成。
- 21 adapters registered ≠ 21 databases real accepted。
- React Foundation 已完成 ≠ Ask Workspace 已完成。
- Backend `plan/evidence.display_command` 已有 ≠ Web Trace/SQL 展示已经完成。
- 当前没有 Excel ingestion、统一 Streaming 或结果图表源码。
- aiyallm 迁移代码已经存在，但仓库没有一份迁移后成功的真实模型回归记录；历史 18/18 验收需要按这个时间边界对外表述。

## 5. Demo Story(路演演示主线)

建议最终 Demo 只展示一条完整故事：

```text
Connect a real database OR upload Excel
→ Scan
→ show Enterprise Data Graph / structure
→ ask a business question
→ stream execution stages
→ show grounded plan and safe SQL/native query
→ execute read-only query
→ show table + chart + evidence
→ show ambiguity clarification once
```

视频应使用已经冻结并通过验收的环境录制，不在录制当天临时更换数据集、模型或连接配置。

## 6. External Wording(对外口径)

可以说：SmartData 已完成可信自然语言问数核心闭环；固定真实 Phase 1 E2E 集 18/18；测试仓库已经准备 16 个服务型数据库和一个容器化 SQLite；Roadshow Release 正在把这些能力产品化为 Web / CLI / Skill / MCP，并补齐 Excel、TLS、Streaming、Trace 与 Visualization。

不要说：总体准确率 100%；21 种数据库全部真实问数验收通过；16 个测试库已经部署到服务器；14 个 SSL 已完成；Excel/Web Streaming/Visualization 已经完成——除非 Roadmap 和真实验收产物之后明确更新为 DONE。
