# Qaneris Documentation(文档导航)

当前 `docs/` 只保留仍需要持续维护的主文档。一次性方案、旧阶段专项说明和已经被新文档吸收的内容不再保留独立文件；历史内容由 Git(版本控制) 保存。

## 1. Authority(权威顺序)

1. **当前实现事实**：以 `main` 分支源码、测试和真实验收产物为准。文档不得把计划写成已完成。
2. [Qaneris Next-Generation Architecture & Implementation Plan](Qaneris_Next_Generation_Architecture_and_Implementation_Plan_v1.0.md)：目标架构与不可破坏的信任边界。
3. [Development Roadmap](development-roadmap.md)：当前进度、Roadshow Release(路演版本)目标与下一工作包。
4. [Semantic Query Engine](semantic-query-engine.md)：Intent(意图)、Retrieval(检索)、Grounding(物理绑定)、Planning(规划)、Validation(校验)与 Execution(执行)边界。
5. [Product Interfaces](product-interfaces.md)：Web / API / CLI / Skill / MCP、Streaming(流式传输)、Trace(执行轨迹)、SQL 展示、Visualization(可视化)与 Excel(电子表格)产品契约。
6. [Excel Ingestion Engineering Specification](excel-ingestion.md)：RS-EXCEL-01 的确定性校验、Sheet→Table、Header/Type、Formula、Artifact、SQLite、幂等与验收规则。
7. [Connection Model](connection-model.md)：数据库连接、Secret(密钥)、SSL/TLS(加密连接)、证书与 CredentialStore(凭据存储)。
8. [Server Test Database Environment](test-database-environment.md)：Qaneris(问数项目)可使用的 17 库服务器测试环境、责任边界与验收流程；不包含 Adapter(适配器) 配置。
9. [Roadshow Overview](roadshow-overview.md)：路演口径、Demo(演示)范围与不能过度承诺的内容。

## 2. Reading Order(阅读顺序)

开发前先读主架构，再按任务读取 `semantic-query-engine.md`、`product-interfaces.md`、`excel-ingestion.md`、`connection-model.md` 或 `test-database-environment.md`，最后看 `development-roadmap.md` 的当前状态。

## 3. Maintenance Rule(维护规则)

- 架构边界变化：更新主架构。
- Query / Grounding 契约变化：更新 `semantic-query-engine.md`。
- Web / CLI / Skill / MCP / Excel / Streaming / Visualization 契约变化：更新 `product-interfaces.md`。
- Excel 具体工程规则变化：更新 `excel-ingestion.md`。
- 数据库连接、认证、SSL/TLS、证书生命周期变化：更新 `connection-model.md`。
- 服务器测试数据库环境用途或责任边界变化：更新 `test-database-environment.md`；完整连接矩阵仍以 `NLQuery-Test-Dataset` 为准。
- 完成度、优先级、Roadshow Scope(路演范围)变化：只更新 `development-roadmap.md`。
- 路演对外口径变化：更新 `roadshow-overview.md`。
- 普通 Bug Fix(缺陷修复)、Refactor(重构)和测试增加不要求同步修改所有文档。
