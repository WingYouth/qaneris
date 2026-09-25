# SmartData Skill 行为示例

## Direct questions

- “7 月已完成订单有多少笔？”：只有一个 ready 数据源时直接调用 `ask_data`，保留原问题，不先索要表名或字段名。
- “销售额最高的前 5 笔订单”：调用 `ask_data`；若结果 `truncated = true`，说明被截断，不推断完整排名。
- “平均订单金额是多少？”：多个数据源都可能包含订单时，先 `list_datasources` 按名称和上下文判断来源。

## Multiple datasource ambiguity

- 两个 ready 数据源都像候选，且名称无法判断：可先用 `search_dataset` 或 `get_schema_context` 缩小范围，仍无法确定时才向用户提最小必要问题。
- 缩小范围后确定唯一来源：直接 `ask_data`，并把已选来源告诉用户。
- 不要为了“确认”每次问题都先 dump 整个 schema。schema 工具是排歧工具，不是流程前置步骤。

## Source setup

- 用户明确给出 `/data/sales.db` 并要求连接：调用 `register_sqlite`，成功后调用 `scan_datasource`。
- 用户只说“连接销售库”但未给路径：询问路径，不猜测文件位置。
- 数据源已经是 `ready`：正常问数时不要重复扫描。

## Secure datasource with a SecretReference

用户：“连接 PostgreSQL 销售库，我已经有 password secret `sec_123`。”

1. 组装 `ConnectionProfile`，认证部分写成引用而非原文：`authentication.method = "password"`，`authentication.password = {"provider": "managed", "identifier": "sec_123"}`。
2. `test_secure_datasource(kind, connection_profile)` — 先测试，不跳过。
3. `create_secure_datasource(name, kind, connection_profile, workspace_id)` — 返回 `status = created`，此时还不能问数。
4. `scan_datasource(datasource_id)` — 进入 `ready`。
5. 告知用户数据源已 ready，可以开始问数。

`environment` / `file` 引用同样可用；Skill 不解析引用背后的内容。

## Secure datasource without a SecretReference

用户：“连接 PostgreSQL，密码是 abc123。”

- **不要把 `abc123` 传给任何 MCP Tool，也不要回显它。**
- 说明：MCP 只消费 `SecretReference`，不接收原始凭据。
- 引导用户先通过现有安全入口创建凭据：`smartdata credential add --kind password`（或 HTTP API `POST /api/credentials`），拿到 `sec_...` 形式的 secret id。
- 拿到引用后再按上一节的 Test → Create → Scan 继续。
- Web 凭据界面尚未可用，不要说“去 Web 上传”。

## Existing datasource status

- `list_datasources` 显示唯一数据源为 `created`：先 `scan_datasource`，成功后 `ask_data`。
- 显示为 `ready`：直接 `ask_data`，不重复 scan。
- 需要确认单个数据源细节：`inspect_datasource(datasource_id)`，它只返回公开字段与扫描事实。

## Update a secure datasource

用户要求更换已有数据源的连接：

1. `test_secure_datasource(candidate profile)`。
2. `update_secure_datasource(datasource_id, connection_profile)`。
3. `scan_datasource(datasource_id)` —— update 使旧 scan 失效，必须重新 scan 才能回到 `ready`。

## Delete

- 只有在用户明确要求删除数据源时才调用 `delete_datasource`。
- 不因为连接失败、scan 失败或 ask 失败而自行删除数据源。

## Clarification

用户：“销售额是多少？”

- `ask_data` 返回 `status = clarification_required`：这是正常产品结果，不是失败。
- 把 SmartData 提出的问题原样转给用户；如果带 `options`，展示最小必要选项。
- 不要自己猜“全部时间”“订单日期”“支付日期”，也不要选一个选项后继续执行。
- 用户回答后，带着原问题回到 `ask_data`。

## Ask progress

- 可能看到 `accepted → grounding_ready → query_ready → execution_started → result_ready → done`。
- 这些是执行状态：SmartData 在理解问题、匹配数据结构、生成安全查询、执行查询。
- 不是模型思维链，不要说“AI 正在推理”。
- 客户端没有展示 progress 时，问数照常执行，这不代表失败。

## Unsupported or unsafe requests

- “修改订单状态”：拒绝执行；SmartData 只提供只读问数。
- “删除重复客户”：拒绝执行，不提交 SQL。
- “把华东和华南两个库的数字加起来”：当前路演范围不支持跨数据源 Join；说明限制，不要分别查询后自行拼接答案。
- 工具返回无数据：说明指定范围内没有记录，不把无数据解释为数值零，除非结果明确返回零。
- 结果被截断：只描述已返回范围，不根据部分行推断全量排名或总数。
- 自然语言问数返回 `unsafe_query` / `model_invocation_failed` 等稳定错误：说明实际问题与最小纠正动作，不声称查询成功，也不改用自写 SQL 绕路。

## Explicit SQL

- 用户给出单条只读 `SELECT` 并明确要求执行：可以把它作为 `ask_data.sql` 提交。
- 用户未要求 SQL，但自然语言问数失败：报告缺失的字段或能力，不要自行构造复杂 Join SQL 绕过限制。
- SQL 包含写入、DDL、PRAGMA 或多个语句：不提交，说明只允许单条只读查询。
