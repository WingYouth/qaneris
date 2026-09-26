# CLI 路演演示脚本（`qaneris shell`）

本文件是**可直接照着敲**的 CLI 演示脚本。演示从 `qaneris shell` 的欢迎面板开始，全程在会话内完成。  
所有命令与预期输出都已在本地 `sales-2026-07`（SQLite）数据源上实测过，标注 `⚠` 的是已知会失败的路径。

- **演示数据源**：`ds_c996e9437e6a` / `sales-2026-07` / `relational` / `sqlite` / `ready` / `scan_version 3` / 4 张表
- **建议时长**：8–12 分钟
- **前置条件**：Neo4j 可连、模型网关已配置（`.env` 里的 `QANERIS_MODEL_*`）、在**仓库根目录**执行

---

## 0. 准备

```bash
cd /Users/wingyouth/qaneris
source .venv/bin/activate
qaneris shell
```

出现欢迎面板即进入会话：

```text
╭─ qaneris shell ──────────────────────────────── Ctrl-C cancel · Ctrl-D exit ─╮
│ ◆ QANERIS  v0.1.0  ·  interactive session over the qaneris command tree      │
│                                                                             │
│ Start here                          Environment                             │
│   doctor      check the runtime env   model       deepseek-v4.1             │
│   source list list datasources        credentials ~/.qaneris/secrets        │
│   ask "..."   ask one question        catalog     ~/qaneris/qaneris.db      │
╰─────────────────────────────────────────────────────────────────────────────╯
```

> **开场话术**：面板本身就在声明边界 —— 它报的是*这个进程将要使用的*模型、凭据目录和目录库，  
> 不是能力清单。`Ctrl-C cancel · Ctrl-D exit` 是会话的两个退出语义。

---

## 1. 环境自检

```text
❯ help
```

打印完整命令树：`doctor / acceptance / ask / source / credential / certificate / shell`。  
**讲点**：`help`（和 `?`）直接复用命令树自己的帮助文本，会话不维护第二份可能过期的副本。

```text
❯ doctor
```

```text
[AVAILABLE] Artifact Root
[CONFIGURED] Model configuration
[CONFIGURED] Neo4j configuration
[AVAILABLE] Neo4j connectivity
[AVAILABLE] Catalog configuration
```

**讲点**：只发布状态，永不输出凭据内容。

```text
❯ doctor --json
```

输出同一份检查的机器可读版本（`checks.<name>.status`）。

---

## 2. 数据源

```text
❯ source list
```

```text
ID               Name           Kind        Driver  Status  Workspace
---------------  -------------  ----------  ------  ------  ---------
ds_c996e9437e6a  sales-2026-07  relational  sqlite  ready   default
```

```text
❯ source list --json
```

```text
❯ source show ds_c996e9437e6a
```

```text
Datasource ID: ds_c996e9437e6a
Name: sales-2026-07
Kind: relational
Driver: sqlite
Status: ready
Workspace: default
Scan version: 3
Last scan status: ready
Dataset count: 4
```

**讲点**：`list` / `show` 只发布 ID / Name / Kind / Driver / Status / Workspace / 扫描状态。  
**存储的连接文档、Secret 引用和任何凭据都不在其中**。

---

## 3. 问数主链（核心）

```text
❯ ask "orders 中 total_amount 的总额"
```

```text
Status: completed
Answer: orders 表中 `total_amount` 的总额为 **312765.9**。
Datasource: ds_c996e9437e6a
Scan version: 3
Rows: 1
Truncated: no

Result:
sum_total_amount
----------------
312765.9

Query:
SELECT SUM(t0."total_amount") AS "sum_total_amount" FROM "orders" AS t0 LIMIT ?

Plan:
Objects: orders
Aggregates: sum(total_amount) AS sum_total_amount
```

**讲点**（照着输出念）：

1. `Answer` 是模型写的，但**数字来自 `Result`**，不是模型算的。
2. `Query` 是**参数化只读 SQL**，`LIMIT ?` 是绑定参数 —— 输出里看不到参数值。
3. `Plan` 是**受治理计划**：`Objects: orders` + `Aggregates: sum(total_amount)`，  
   没有出现原生 SQL 文本。

```text
❯ ask "orders 的总记录数是多少？"
```

→ `62`

```text
❯ ask "orders 中 total_amount 的平均值"
```

→ `5044.611290322581`（Answer 里会写"约 5044.61"）

```text
❯ ask "What tables are in this database?"
```

→ 结构盘点：4 张表（`customers` / `orders` / `order_items` / `products`）、22 个字段。  
**讲点**：这条走的是**已发布图结构**，不读表内记录；模型总结失败时盘点结果仍然可用。  
回答里会明确写"是结构信息，不是表内记录"。

---

## 4. 流式事件

```text
❯ ask "orders 中 total_amount 的总额" --stream
```

```text
[accepted] Request accepted
[intent] Intent ready
[retrieval] Retrieval ready (candidates=14)
[grounding] Grounding ready (executable=yes)
[plan] Plan ready
[query] Query ready
[execution] Execution started
[result] Result ready
[done] Completed
```

```text
❯ ask "orders 中 total_amount 的总额" --stream --json
```

严格 JSONL：一行一个 `AskEvent`，`sequence` 从 1 递增，`done` 恒为最后一行。

**讲点**：`--stream --json` 里 `grounding_ready` 的 payload 能看到  
`total_amount → orders.total_amount` 的物理绑定和 `obj_c4e812fd625674f929f16363` 这样的图节点 id ——  
**这是"不是让大模型直接写 SQL"最直接的证据**。事件只暴露执行事实，不含模型私有推理。

---

## 5. 凭据生命周期（安全边界）

```text
❯ credential add --kind password
```

输入被隐藏（getpass 无回显），返回：

```json
{
  "status": "completed",
  "credential": {
    "secret_id": "sec_xxxxxxxx",
    "kind": "password",
    "created_at": "2026-09-26T08:19:12.057005Z",
    "metadata": {}
  }
}
```

**讲点**：命令树里**不存在** `--password VALUE` / `--token VALUE` / `--api-key VALUE` /  
`--secret VALUE` / `--value VALUE`。只有两个输入通道：隐藏交互输入，或显式 `--stdin` 管道。  
一条都没法把密钥写进 argv，所以 shell 历史里也不会留下密钥。

```text
❯ credential show sec_xxxxxxxx
```

```text
Secret ID: sec_xxxxxxxx
Kind: password
Created: 2026-09-26T08:19:12.057005+00:00
```

**讲点**：只回 kind 和时间，**不回显值**。

```text
❯ credential delete sec_xxxxxxxx
```

```text
[PASS] Credential deleted
Secret ID: sec_xxxxxxxx
```

---

## 6. 受治理的拒绝（fail-closed）

```text
❯ ask "已完成订单按商品分类统计销售额，哪个分类最高？"
```

```text
Status: clarification_required
Answer: 当前问题需要连接多个数据对象，现有查询能力只支持一条已确认的直接关系。
```

**讲点**：`clarification_required` 是**正常产品结论**，退出码是 **0**（看右下角状态行），  
不是错误。系统宁可追问也不猜一个没被治理的 join。

```text
❯ ask "2026 年 7 月 1 日到 5 日每天已完成订单的销售额是多少？" --stream
```

```text
[accepted] Request accepted
[intent] Intent ready
[retrieval] Retrieval ready (candidates=12)
[grounding] Grounding ready (executable=yes)
[clarification] Clarification required
  Question: 请明确唯一的业务时间维度（例如下单日期、支付日期或发货日期）。
    - 日期
    - 下单日期
[done] Clarification required
```

**讲点**：时间轴有歧义时给出**可选项**而不是挑一个。这就是"受治理"的含义。

---

## 7. Shell 特性（穿插讲，不用单独敲）

| 特性       | 现象                                                                                          |
| -------- | ------------------------------------------------------------------------------------------- |
| 状态行      | 右下角 ` qaneris <上一条命令>  exit N  run N   help · Ctrl-D exit`；退出码按语义着色（0 绿 / 1、2 红 / 130 中性）   |
| 退出码不结束会话 | 命令的退出码只是状态行上的一个事实，会话本身正常结束返回 0                                                              |
| 前缀自动剥离   | `qaneris source list` 与 `source list` 等价（可重复剥离），README 里的命令可直接粘贴                            |
| 参数写错不崩   | argparse 的 `SystemExit`（含 `--help`）在 dispatch 边界被转成退出码；现场敲一个 `ask` 试                        |
| Ctrl-C   | 取消当前命令 → `▸ cancelled — the session is unchanged`（退出码 130）；空提示符下只清空当前行                      |
| 不嵌套      | 敲 `shell` 只打印 shell 自己的帮助，不会起第二层会话                                                          |
| 引号未闭合    | `▸ cannot parse unmatched quote`，会话继续                                                       |
| 无驱动噪声    | 会话把 `neo4j.notifications` 提到 ERROR，屏幕上不会出现一次性命令里那些 `Received notification from DBMS server` |
| 历史       | `.tools/shell_history`（目录 0700 / 文件 0600，已 gitignore），因为会话里的问题可能描述业务数据                      |
| 内建命令     | `?` `help` `clear` `exit` `quit`；**只在不是真实命令时才生效**，所以永远不会遮蔽命令树                               |

---

## 8. 退出

```text
❯ exit
```

（或 `quit`、Ctrl-D）

```text
goodbye · history saved to /Users/wingyouth/qaneris/.tools/shell_history
```

---

## 9. 已知限制（演示前必读）

以下路径在**当前数据源上必然失败或走澄清**，不要当作成功案例演示：

| 现象                                                                    | 原因                                                                                        |
| --------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| `⚠` 任何带时间表达式的问句 → `Ask failed: 时间维度"下单日期"绑定到非日期字段 order_date`（exit 1） | `orders.order_date` 在 SQLite 里被扫成 `TEXT`，而语义资产声明了 `time_axis: true`，契约要求 date/datetime    |
| `⚠` "已完成订单的销售总额" → `null`                                             | 中文过滤值 `已完成` 被当作字面量绑定，实际列值是 `completed`；缺中文值映射                                             |
| `⚠` "total_amount 的最大值" → 澄清                                          | 指标资产的 `default_aggregation` 是 `sum`，max/min 未解析到该指标                                       |
| `⚠` "total_amount 最高的前 5 条记录" → 错误计划                                  | 规划器产出 `SUM(...) ORDER BY sum_... DESC`，模型随后诚实拒绝作答                                         |
| `⚠` 多表 join 超过 1 跳 → 澄清                                               | Phase 1 限制：只支持一条已确认的直接关系                                                                  |
| `⚠` `acceptance phase1`                                               | 本机未验证：会写 `~/QanerisArtifacts/acceptance/phase1/<时间戳>/report.json`，需在沙箱/权限允许的终端里跑一次再决定是否演示 |

**演示口径**：以上第 2–5 条如果要讲，就明确说成"系统的诚实边界"（宁可澄清/拒绝，也不给错数字），  
不要说成"支持"。

---

## 附：一次性命令版本（不用 shell）

```bash
cd /Users/wingyouth/qaneris
Q=ds_c996e9437e6a

qaneris doctor
qaneris source list
qaneris source show $Q
qaneris ask "orders 中 total_amount 的总额" --datasource $Q
qaneris ask "orders 的总记录数是多少？" --datasource $Q
qaneris ask "orders 中 total_amount 的平均值" --datasource $Q
qaneris ask "What tables are in this database?" --datasource $Q
qaneris ask "orders 中 total_amount 的总额" --datasource $Q --stream
qaneris ask "orders 中 total_amount 的总额" --datasource $Q --stream --json
qaneris ask "已完成订单按商品分类统计销售额，哪个分类最高？" --datasource $Q
printf '%s' 'demo-password-123' | qaneris credential add --kind password --stdin
```

> 一次性命令会输出 Neo4j 驱动的 `Received notification from DBMS server` 噪声。  
> 它在 **stderr**，stdout 是干净的（实测一次问数：stdout 18 行、stderr 22 行）。  
> 想让屏幕干净就加 `2>/dev/null`；`--json` 场景下 CLI 自己会把杂散输出重定向到 stderr。
