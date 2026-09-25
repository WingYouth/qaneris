# tmp/ — Phase 1 真实 LLM E2E 验收归档

本目录保存 **2026-09-20 Phase 1 真实环境端到端验收** 留下的文件。
它是归档，不是临时目录（目录名沿用了当时的叫法）。

## 最重要的一份：`phase1_e2e_acceptance_report.md`

**先读这一份。** 它是那次**真实调用大模型**的验收报告，包含：

- 环境（真实 OpenAI 兼容端点 + 真实 Neo4j + 真实 SQLite，不含任何凭据）
- 验收 scope（SQLite 表结构、9 个受治理语义资产及其物理绑定）
- 六类问题的 BusinessQuery 形态、状态、行数与数值
- 逐类证据（每个场景的 SQL、参数元组、结果、澄清载荷）
- 独立校验（与纯 SQLite 计算逐项对照）
- **未保留的证据项**与当前环境状态

## 各文件来源（重要，请勿混淆）

| 文件 | 来源 | 说明 |
| --- | --- | --- |
| `phase1_e2e_acceptance_report.md` | 大模型可用期间的那次验收 | **主报告**，由当时的持久记录整理 |
| `source.db` | 验收 scope | SQLite 源库（customers 42 / orders 360），可由脚本重建 |
| `catalog.db` | 验收 scope | catalog + 9 个受治理语义资产（含图物理身份），可由脚本重建 |
| `profiles/phase1-e2e-sqlite/database_profile.md` | 验收 scope | 扫描产生的数据源画像 |
| `phase1_e2e_report.json` | **端点已不可用时**的 harness 输出 | 六题均为 `aborted`，请当作「端点状态证据」而非验收结果 |
| `phase1_e2e_report.txt` | 同上 | 那一轮的 stdout 全文 |

> 模型账户额度耗尽后（`429 insufficient balance`）重跑失败，才产生 JSON/TXT 这两个
> `aborted` 文件。它们被保留下来，是为了同时记录两件事：端点当时的真实状态，以及
> 加固后的 harness 能把失败原因写进报告。**它们不代表验收结果**——验收结果是那 18/18 轮次，
> 记录在 `phase1_e2e_acceptance_report.md` 里。

## 如何复现完整验收

额度恢复、Neo4j 在 `bolt://localhost:7687` 可用之后，一条命令：

```bash
cd /Users/wingyouth_is01/code/SmartData
python3 -m smartdata.scripts.acceptance.phase1_e2e
```

报告默认写到 `SmartDataArtifacts/acceptance/phase1/phase1_e2e_report.json`
（该目录在项目之外，是项目的产物约定）。单题失败不会中断整轮，且每题结束即重写报告。

## 时间线说明

本目录记录的是 **MODEL-01（aiyallm 模型网关迁移）之前**的验收：报告里描述的调用链是
`httpx POST {base_url}/chat/completions`。迁移后该链路已改为经由 `aiyallm`，
报告内容作为当时的事实保持原样，未做追改。当前实现见 `docs/model-gateway.md` §12。

## 关于版本控制

- 本目录**纳入 git**，因为这是需要长期留存的验收证据；
  项目其它产物仍按约定放在项目目录之外（`artifact_root()` 强制）。
- `profiles/` 在项目 `.gitignore` 中被通配忽略（该规则针对本机扫描产物）。
  这里是用 `git add -f` 强制加入的；若不需要随仓库保存，`git rm -r --cached tmp/profiles` 即可。
- 入库前已核对：这些文件中**不含**任何 API key、密码或连接凭据。
