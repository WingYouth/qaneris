# 2026 年 7 月销售演示数据

本目录提供 SmartData 的确定性 SQLite 验收数据。仓库保存 SQL 和问题契约，不保存生成后的数据库二进制文件。

## 生成数据库

在仓库根目录执行：

```bash
python examples/create_demo_database.py
```

默认生成 `examples/sales_2026_07.db`。指定其他路径或替换已有文件：

```bash
python examples/create_demo_database.py /tmp/sales.db
python examples/create_demo_database.py /tmp/sales.db --force
```

## 数据内容

- 10 个客户，覆盖华东、华北、华南、西部和华中。
- 8 个商品，覆盖电脑、配件、音频和存储。
- 2026 年 7 月 1 日至 31 日每天 2 笔订单，共 62 笔。
- 每笔订单 2 条明细，共 124 条。
- 订单状态包含 `completed`、`pending` 和 `cancelled`。
- `orders.total_amount` 与对应明细金额之和保持一致。

## 问题契约

[`questions_2026_07.json`](questions_2026_07.json) 为 Web、MCP、Skill 共用的验收契约：

- `question`：用户自然语言问题。
- `verification_sql`：只用于验证标准答案的只读 SQL。
- `expected_rows`：标准结果。
- `supported_now`：当前规则规划器是否已经支持该问题。

当前 12 条标准问题均已支持。多表能力仅覆盖契约中的商品分类销售额、客户地区销售额和商品销量排名，不代表系统具备任意 Join 规划能力。
