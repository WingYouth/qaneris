# IQ-02 — Five-Database Grounded Execution(五数据库可信执行)

**Status(状态)：READY FOR CODEX IMPLEMENTATION(可交给 Codex 代码智能体直接实施)**  
**Implementation Baseline(实现基线)：`main(主分支)@3c1697284e36a1de8f2e334a117e6b2b670d667e`**  
**Prerequisite(前置卡)：IQ-01 Reliable Semantic Ask(可靠语义问数) = PASS**  
**Parent Plan(父级计划)：`docs/conversational-analytics-runtime-development-plan.md`**  
**Scope(范围)：只实现 IQ-02(五数据库可信执行)，不得提前进入 IQ-03/IQ-04/IQ-05/IQ-06**

---

## 1. 目标

本卡把当前 Formal Grounded Query(正式落地查询) 从 SQLite-only(仅 SQLite) 执行链扩展为以下五个 Release Gate(发布门) 数据库：

```text
SQLite
PostgreSQL
MySQL
MongoDB
Redis
```

“支持”必须表示完整经过：

```text
BusinessQuery(业务查询)
→ Semantic Retrieval(语义检索)
→ Grounding(语义落地)
→ GroundedQueryContext(落地查询上下文)
→ GroundedQueryPlan(落地查询计划)
→ GroundedPlanValidator(落地计划校验)
→ GroundedNativeCompiler(落地原生查询编译)
→ GroundedNativeQueryValidator(落地原生查询校验)
→ Revision Fence(扫描版本围栏)
→ Read-only Adapter Execution(只读适配器执行)
→ GroundedQueryResult + ExecutionEvidence(落地结果 + 执行证据)
```

本卡不允许模型直接产生可执行 SQL(结构化查询语言)、MongoDB Pipeline(MongoDB 管道) 或 Redis Command(Redis 命令)。

---

## 2. 当前真实代码状态

Codex(代码智能体) 开始前必须重新阅读真实代码，至少：

```text
qaneris/contracts/query.py
qaneris/application/service.py
qaneris/querying/generation/grounded_sql.py
qaneris/querying/generation/generator.py
qaneris/querying/execution.py
qaneris/querying/validation/native.py
qaneris/querying/validation/read_only.py
qaneris/adapters/base.py
qaneris/adapters/relational/sqlite.py
qaneris/adapters/relational/sqlalchemy.py
qaneris/adapters/document/mongodb.py
qaneris/adapters/key_value/redis.py
qaneris/adapters/registry.py
docs/test-database-environment.md
```

当前必须认识到：

1. `GroundedSQLCompiler(落地 SQL 编译器)` 仍固定生成 SQLite `?` placeholder(占位符)。
2. `GroundedNativeQueryValidator(落地原生查询校验器)` 当前只接受 `QueryLanguage.SQL`。
3. `NativeQuery.command` 当前只能是 `str`。
4. `GroundedQueryExecutor(落地查询执行器)` 永远调用 `adapter.execute_bound()`。
5. `SQLiteAdapter(SQLite 适配器)` 实现了 `execute_bound()`。
6. `SQLAlchemyAdapter(SQLAlchemy 适配器)` 没有正式 `execute_bound()`。
7. `MongoDBAdapter(MongoDB 适配器)` 和 `RedisAdapter(Redis 适配器)` 目前只在 legacy execute(旧执行入口) 接受 JSON string(JSON 字符串)。
8. 旧 `QueryGenerator(查询生成器)` 虽已有 MongoDB/Redis 分支，但它属于 Profile-era Compatibility Path(旧 Profile 兼容链)，**不能作为 Formal Grounded Query(正式落地查询) 的实现捷径**。

---

## 3. 冻结边界

### 3.1 本卡允许

允许新增/修改：

```text
qaneris/contracts/query.py
qaneris/querying/generation/*
qaneris/querying/validation/*
qaneris/querying/execution.py
qaneris/adapters/base.py
qaneris/adapters/relational/sqlite.py
qaneris/adapters/relational/sqlalchemy.py
qaneris/adapters/document/mongodb.py
qaneris/adapters/key_value/redis.py
qaneris/application/service.py
相关 tests(测试)
必要 docs(文档)
```

推荐新增：

```text
qaneris/querying/generation/native.py
qaneris/querying/generation/grounded_mongo.py
qaneris/querying/generation/grounded_redis.py
```

### 3.2 本卡禁止

不得实现：

- Conversation(会话)
- Semantic Memory(语义记忆)
- Diagnostic Agent(诊断智能体)
- FederatedPlan(联合计划)
- 跨数据源 Join(关联)
- JoinMapping(关联映射)
- 新权限体系
- 任意模型生成 Native Query(原生查询)
- 为五个数据库建立五套彼此独立的 Ask(问数) 流水线

### 3.3 单源安全边界保持不变

`GroundedQueryPlan(落地查询计划)` 继续：

```text
一个 plan_id
一个 datasource_id
一个 scan_version
```

IQ-02 不允许把一个 `GroundedQueryPlan` 改成多数据源计划。

---

## 4. 目标执行架构

统一为：

```text
GroundedQueryPlan
        ↓
GroundedNativeCompiler
        ├─ sqlite      → GroundedSQLCompiler(sqlite)
        ├─ postgresql  → GroundedSQLCompiler(postgresql)
        ├─ mysql       → GroundedSQLCompiler(mysql)
        ├─ mongodb     → GroundedMongoCompiler
        └─ redis       → GroundedRedisCompiler
        ↓
NativeQuery
        ↓
GroundedNativeQueryValidator
        ↓
Revision Fence
        ↓
GroundedQueryExecutor
        ↓
DataSourceAdapter.execute_native
        ├─ relational → execute_bound
        ├─ mongodb    → typed read payload
        └─ redis      → typed read payload
```

Application Service(应用服务) 只负责：

1. 从 Catalog(目录) 获取真实 Datasource(数据源) 的 `kind/driver`；
2. 把 `GroundedQueryPlan` 与 Datasource Target(数据源目标) 交给 compiler(编译器)；
3. 再执行现有 Validation / Revision / Executor(校验 / 版本围栏 / 执行器)。

Compiler(编译器) 不自己查 Catalog(目录)、Neo4j(图数据库) 或模型。

---

## 5. NativeQuery(原生查询) 契约

当前 `NativeQuery.command: str` 无法承载 MongoDB/Redis 的 typed native payload(类型化原生负载)。

本卡把正式契约调整为：

```python
class NativeQuery(BaseModel):
    datasource_id: str
    plan_id: str
    scan_version: int | None
    query_language: QueryLanguage

    command: str | dict[str, Any]
    parameters: tuple[Any, ...] = ()

    # 始终是可公开、安全、无业务参数明文的字符串
    display_command: str
```

规则：

- SQL(结构化查询语言)：`command` 是 SQL string(SQL 字符串)，业务值只放 `parameters`。
- MongoDB/Redis：`command` 是内部 typed dict(类型化字典)；只存在 Runtime Memory(运行内存) 内，不直接公开。
- `display_command` 必须是 canonical safe rendering(规范安全展示)，把业务值替换为 placeholder(占位符)。
- Public SSE/API/Evidence(公开 SSE/API/证据) 只允许输出 `display_command`，不得输出 `command` 或 `parameters`。
- Provenance(来源校验) 必须比较真实 `command` 与 `parameters`，不是只比较 display text(展示文本)。

不要把业务参数塞进 `display_command`。

---

## 6. Adapter Execution Port(适配器执行端口)

在 `DataSourceAdapter(数据源适配器)` 增加一个统一正式执行端口：

```python
def execute_native(
    self,
    command: str | dict[str, Any],
    parameters: tuple[Any, ...] = (),
    max_rows: int = 200,
) -> NormalizedResult:
    ...
```

默认行为：

- `command is str` → 调用 `execute_bound()`；
- typed dict(类型化字典) 如果适配器未实现 → Fail Closed(失败关闭)。

`GroundedQueryExecutor` 统一只调用 `execute_native()`。

Legacy `execute(query: str)` 保留兼容，不删除。

这样：

- SQLite/PostgreSQL/MySQL 正式执行仍走参数化 SQL；
- MongoDB/Redis 正式执行走 typed payload(类型化负载)；
- Executor(执行器) 不需要 import(导入) 五种具体 Adapter(适配器)。

---

## 7. Relational(关系型) — SQLite / PostgreSQL / MySQL

### 7.1 GroundedSQLCompiler(落地 SQL 编译器)

改成显式接收 locked driver(锁定驱动)：

```python
compile(plan, driver="sqlite" | "postgresql" | "mysql")
```

只支持这三个 driver(驱动) 进入 IQ-02 Formal Path(正式路径)。

方言：

```text
SQLite:
  identifier quote = "
  placeholder = ?

PostgreSQL:
  identifier quote = "
  placeholder = %s

MySQL:
  identifier quote = `
  placeholder = %s
```

三者当前范围都使用 `LIMIT placeholder`。

Business Value(业务值) 必须始终留在 `parameters`，禁止重新把值拼进 SQL string(SQL 字符串)。

保留现有：

- NULL equality semantics(NULL 等值语义)
- IN
- BETWEEN
- CONTAINS / LIKE
- time range(时间范围)
- aggregate(聚合)
- group by(分组)
- sort(排序)
- confirmed direct join(已确认直接关联)
- bounded limit(有界限制)

### 7.2 SQLAlchemyAdapter(SQLAlchemy 适配器)

实现正式 `execute_bound()`。

推荐使用 SQLAlchemy `exec_driver_sql(query, parameters)`，因为正式 compiler(编译器) 输出的是目标 DBAPI(driver API) 参数形式：

- psycopg(PostgreSQL 驱动)：`%s`
- pymysql(MySQL 驱动)：`%s`

不要使用字符串替换把参数写入 SQL。

正式执行至少拒绝 IQ-02 未锁定的 SQLAlchemy driver(驱动) 进入此新 Formal Path(正式路径)，避免 Oracle/SQL Server/ClickHouse 等被误宣称已完成。

Legacy `execute()` 行为保持现有兼容。

---

## 8. MongoDB(文档数据库)

新增 `GroundedMongoCompiler(落地 MongoDB 编译器)`。

输入只能是 validated `GroundedQueryPlan`。

### 8.1 单 Collection(集合) 边界

IQ-02 MongoDB 不实现 `$lookup`。

因此：

```text
len(plan.data_object_ids) == 1
plan.joins == []
```

否则 Fail Closed(失败关闭)。

### 8.2 Find(查找) 计划

没有 aggregate/group_by(聚合/分组) 时产生：

```json
{
  "operation": "find",
  "collection": "...",
  "filter": {...},
  "projection": {...},
  "sort": [...],
  "limit": 200
}
```

collection(集合) 必须来自 `plan.data_objects` 的真实 native locator(原生定位)。

Field Path(字段路径) 只能来自 Grounded Plan(落地计划)。

### 8.3 Aggregate(聚合) 计划

存在 aggregates/group_by(聚合/分组) 时产生只读 pipeline(管道)，只允许：

```text
$match
$group
$project
$sort
$limit
$count
```

Aggregate(聚合) 映射：

```text
COUNT → $sum: 1 或 $count
SUM   → $sum
AVG   → $avg
MIN   → $min
MAX   → $max
```

支持：

- filter(过滤)
- time range(时间范围)
- selected projection(选择投影)
- group_by(分组)
- aggregate sort(聚合排序)
- field sort(字段排序)
- limit(限制)

CONTAINS(包含) 使用 escaped literal regex(转义字面正则)，不能把用户输入当任意正则程序。

### 8.4 Mongo Validation(Mongo 校验)

Formal Validator(正式校验器) 独立检查：

- operation 只允许 find/aggregate；
- collection 与 plan native locator(计划原生定位) 一致；
- 所有字段都属于 plan allowlist(计划允许列表)；
- pipeline stage(管道阶段) 在白名单；
- 递归拒绝：
  - `$out`
  - `$merge`
  - `$where`
  - `$function`
  - `$accumulator`
- limit 必须有界，且不能超过 plan.limit；
- provenance(来源) 与 canonical expected compile(规范预期编译) 完全一致。

MongoDBAdapter(MongoDB 适配器) 的 legacy JSON parse(JSON 解析) 可以保留，但 Formal Path(正式路径) 应直接消费 typed dict(类型化字典)，不要 stringify(JSON 字符串化) 后再 parse(解析)。

---

## 9. Redis(内存键值数据库)

Redis 不能伪装成关系数据库。

IQ-02 的原则：

> Formal Grounding(正式语义落地) 能表达、Redis 数据模型能安全执行的才执行；不能表达的 Group By(分组)、任意 Join(关联)、任意聚合必须明确 Fail Closed(失败关闭)。

### 9.1 Redis Scan Schema(扫描结构)

当前 Redis Metadata(元数据) 每种 type(类型) 只有虚拟 `key` 字段。

IQ-02 允许增加 Adapter-owned Virtual Fields(适配器拥有的虚拟字段)，用于 Formal Plan(正式计划)：

```text
key
value
type
ttl
exists
cardinality
```

这些不是 Redis 真正 hash column(hash 列)，必须在代码注释和测试中明确是 Adapter Virtual Field(适配器虚拟字段)。

它们只描述安全只读读取能力，不复制业务值到 Graph(图数据库)。

### 9.2 GroundedRedisCompiler(落地 Redis 编译器)

Compiler(编译器) 根据：

- object_kind(redis string/hash/list/set/zset)
- selected virtual field(选中虚拟字段)
- key equality / key IN filter(key 等值 / IN 过滤)
- plan.limit

确定性选择命令。

最低映射：

```text
string + key = X + value       → GET X
string + key IN [...] + value  → MGET ...

hash + key = X + value         → HGETALL X
list + key = X + value         → LRANGE X 0 (limit-1)
set + key = X + value          → SMEMBERS X
zset + key = X + value         → ZRANGE X 0 (limit-1) WITHSCORES

key listing                      → SCAN (bounded, type-scoped when supported)
selected exists + key = X        → EXISTS X
selected type + key = X          → TYPE X
selected ttl + key = X           → TTL X
selected cardinality + set       → SCARD X
selected cardinality + zset      → ZCARD X
```

`HGET` 可以继续属于 Adapter Read Allowlist(适配器读取白名单)，但 Formal Compiler(正式编译器) 只有在 Grounded Plan(落地计划) 明确拥有一个受治理 hash member field(hash 成员字段) 时才允许生成；不能猜 hash member name(hash 成员名)。

### 9.3 Redis Formal Result(正式结果)

Formal Adapter(正式适配器) 必须把原生命令结果转成 bounded `NormalizedResult`。

尤其不能让：

```text
SCAN → [cursor, [key1, key2, ...]]
```

以一个 opaque value(不透明值) 行返回。

Formal SCAN(正式扫描) 要归一化为：

```text
columns = ["key"]
rows = [{"key": ...}, ...]
```

并在 `max_rows` 达到后停止。

GET/MGET/HGETALL/LRANGE/SMEMBERS/ZRANGE 等也必须保持 `max_rows` / bounded value(有界值) 语义。

### 9.4 Redis Validation(Redis 校验)

正式 Validator(校验器) 必须独立确认：

- command 在 read-only allowlist(只读白名单)；
- key/args 来自 canonical compiler(规范编译器)；
- 不出现任意 `execute_command(user_text)`；
- SCAN COUNT/循环有边界；
- range end(范围终点) 不超过 plan/max_rows 预算；
- provenance 与 canonical expected compile 完全一致。

---

## 10. GroundedNativeCompiler(落地原生编译器)

新增一个小型 dispatch layer(分派层)，不要使用 Plugin Framework(插件框架) 或复杂注册机制。

建议：

```python
class GroundedNativeCompiler:
    def compile(
        self,
        plan: GroundedQueryPlan,
        datasource: Datasource,
    ) -> NativeQuery:
        ...
```

只允许：

```text
(relational, sqlite)
(relational, postgresql)
(relational, mysql)
(document, mongodb)
(key_value, redis)
```

其它 driver(驱动)：

```text
QueryPlanningError:
formal grounded execution is not enabled for this driver
```

不要因为 Registry(注册表) 里有 Oracle/SQLServer/CouchDB 等就让它们进入 IQ-02 正式路径。

---

## 11. GroundedNativeQueryValidator(落地原生查询校验器)

从 SQL-only validator(SQL-only 校验器) 升级为 language-dispatched validator(按语言分派校验器)，但保持一个 public entry point(公共入口)：

```python
validate(query, plan, expected=...)
```

至少支持：

```text
QueryLanguage.SQL
QueryLanguage.MONGODB_JSON
QueryLanguage.REDIS_JSON
```

公共校验：

- datasource_id
- plan_id
- scan_version
- query_language
- canonical provenance(command + parameters)

SQL 专属：

- read-only SQL
- 单 statement(单语句)
- placeholder count 与 parameters 对齐
- 禁止混用 `?` / `%s`

Mongo 专属：

- typed payload shape
- collection/field allowlist
- stage/operator allowlist
- bounded limit
- forbidden recursive keys

Redis 专属：

- typed payload shape
- read command allowlist
- bounded arguments
- canonical command selection

---

## 12. Application Service(应用服务) 改造

当前：

```python
self.grounded_sql_compiler = GroundedSQLCompiler()
```

改成以一个 `GroundedNativeCompiler(落地原生编译器)` 为主入口。

`generate_grounded_query(plan)`：

1. 从 Catalog(目录) 根据 `plan.datasource_id` 获取 Datasource(数据源)；
2. 编译 canonical NativeQuery(规范原生查询)；
3. independent validate(独立校验)；
4. 返回 NativeQuery。

`execute_grounded_plan(context, plan,...)`：

1. validate plan/context；
2. 获取 Datasource 并验证 workspace；
3. compile canonical expected；
4. validate native + provenance；
5. 读当前 graph scan_version；
6. revision validate；
7. executor.execute_native；
8. result/evidence。

不要把 driver-specific if/else(driver 条件分支) 散落到 Service(服务) 中。

---

## 13. Evidence(证据) 与公开 Trace(轨迹)

现有 `ExecutionEvidence.display_command` 保持字符串。

示例：

SQL：

```text
SELECT ... WHERE "region" = ? LIMIT ?
```

Mongo：

```json
{"operation":"find","collection":"orders","filter":{"region":"<param>"},"limit":200}
```

Redis：

```json
{"command":"GET","args":["<param>"]}
```

不得公开：

- SQL parameters
- Mongo filter values
- Redis key values，如果它们来自敏感业务输入
- Credential/Secret/TLS material

现有 `_native_query_payload()` 继续只输出 `display_command`。

---

## 14. 测试矩阵

### 14.1 Contract / Compiler Unit Tests(契约 / 编译器单测)

新增或扩展：

```text
tests/unit/querying/test_grounded_sql_compiler.py
tests/unit/querying/test_grounded_mongo_compiler.py
tests/unit/querying/test_grounded_redis_compiler.py
tests/unit/querying/test_grounded_native_validator.py
```

必须覆盖：

SQL：
- SQLite quoting/placeholders
- PostgreSQL quoting/%s
- MySQL backtick/%s
- malicious string stays parameterized
- IN/BETWEEN/CONTAINS
- time range
- join
- group/sort/aggregate
- unsupported driver fail closed

Mongo：
- find
- aggregate
- group
- time filter
- sort/limit
- forbidden stage rejected
- forged collection rejected
- user regex escaped
- no $lookup

Redis：
- GET
- MGET
- HGETALL
- LRANGE
- SMEMBERS
- ZRANGE
- SCAN bounded
- EXISTS
- TYPE
- TTL
- SCARD
- ZCARD
- unsupported relational plan fails closed
- write command impossible

### 14.2 Adapter Unit Tests(适配器单测)

SQLAlchemyAdapter：
- execute_bound passes SQL + tuple parameters to driver API
- never interpolates value into SQL
- fetch max_rows+1 and truncates
- query safety validation remains active

MongoDBAdapter：
- typed Formal Payload(正式负载) works without JSON stringify/parse roundtrip
- read-only restrictions still enforced
- max_rows enforced

RedisAdapter：
- typed Formal Payload works
- Formal SCAN produces row-per-key
- collections/ranges bounded
- legacy execute(query string) still passes existing tests

### 14.3 Formal Integration Tests(正式集成测试)

必须新增至少：

```text
SQLite formal grounded execution
PostgreSQL formal grounded execution
MySQL formal grounded execution
MongoDB formal grounded find
MongoDB formal grounded aggregate
Redis formal grounded exact-key read
Redis formal grounded bounded key scan
```

测试必须经过：

```text
GroundedQueryPlan
→ compiler
→ validator
→ executor
→ adapter
→ GroundedQueryResult
→ ExecutionEvidence
```

不能直接调用 adapter 后宣布 Formal E2E PASS。

### 14.4 Real Environment Acceptance(真实环境验收)

仓库已有：

`docs/test-database-environment.md`

外部测试环境 Source of Truth(唯一事实来源)：

`JingJIang96200/NLQuery-Test-Dataset`

如果当前执行环境能取得必要凭据/证书，则运行五库真实验收。

如果无法取得凭据、mTLS 证书或网络不可达：

```text
unit/integration = PASS
real environment = BLOCKED
```

禁止使用 Mock(模拟) 结果代替真实环境 PASS。

注意已有服务器环境说明：

- PostgreSQL 使用真实数据库；
- MySQL 使用真实数据库；
- MongoDB 使用真实数据库；
- Redis 使用真实数据库；
- PostgreSQL/MongoDB/Redis 环境涉及 mTLS；
- 密码、证书和私钥不得提交仓库。

---

## 15. IQ-01 Regression(IQ-01 回归)

IQ-02 必须保持：

- Low Confidence(低置信度) 不重新成为硬门；
- Grounding(语义落地) 真实候选澄清；
- Missing Data(缺失数据) 具体解释；
- Answer Composer(答案编排器)；
- Answer Model(答案模型) 失败降级；
- Number Guard(数字保护)。

不要为了五数据库执行修改 IQ-01 产品行为。

---

## 16. Codex(代码智能体) 实施顺序

### Step 1 — Generalize Formal Native Contract(泛化正式原生契约)

先完成：

- `NativeQuery.command` typed contract；
- `DataSourceAdapter.execute_native`；
- Executor 改用统一 execute_native；
- Validator 公共骨架。

先确保 SQLite 全部旧测试继续 PASS。

### Step 2 — Relational Three(三个关系库)

完成：

- driver-aware GroundedSQLCompiler；
- SQLAlchemyAdapter.execute_bound；
- PostgreSQL/MySQL placeholder + quoting；
- SQL validator placeholder handling。

先跑 SQLite/PostgreSQL/MySQL targeted tests(针对性测试)。

### Step 3 — MongoDB

完成 compiler + validator + adapter typed formal execution。

不要先碰 Redis。

### Step 4 — Redis

完成 virtual field(虚拟字段) 结构、compiler、validator、bounded normalized execution。

### Step 5 — Application Orchestration(应用编排)

把 Service 从 `GroundedSQLCompiler` 切换到 `GroundedNativeCompiler`。

确保 `ask()` / `ask_stream()` 事件类型不改变。

### Step 6 — Full Regression(全量回归)

必须运行：

```bash
pytest -q
ruff check .
```

然后根据环境尝试五库 real acceptance(真实验收)。

---

## 17. Acceptance Scenarios(验收场景)

### A. SQLite

包含 filter + aggregate + limit(过滤 + 聚合 + 限制)，正式链 PASS。

### B. PostgreSQL

同类 Grounded Plan(落地计划) 编译为 PostgreSQL-safe SQL，值不出现在 command/display evidence，真实参数化执行 PASS。

### C. MySQL

Identifier(标识符) 使用 backtick(反引号)，参数仍绑定，真实执行 PASS。

### D. MongoDB Find

基于真实 collection/field grounding，typed find payload 执行并返回 typed rows/evidence。

### E. MongoDB Aggregate

SUM/COUNT + group + filter 走只读 pipeline；`$out/$merge` 无法进入正式执行。

### F. Redis Exact Key

受治理 key/value query(键/值查询) 编译到正确只读命令并返回 normalized rows。

### G. Redis Bounded Scan

SCAN 不得返回 opaque cursor blob；必须返回 bounded row-per-key，超过预算截断/停止。

### H. Forged Native Query

同 datasource/plan/scan_version，但修改 command/parameters：

```text
必须被 provenance validator 拒绝
```

SQL/Mongo/Redis 都要覆盖。

### I. Stale Scan Version

五库正式执行都必须在打开业务连接前被 Revision Fence 拒绝。

### J. Public Evidence

Filter value、Redis key、Mongo predicate value、SQL parameters 不得泄露到 public event/evidence。

---

## 18. Definition of Done(完成定义)

只有全部满足，IQ-02 才能标记 DONE：

1. 正式 compiler 主入口不再是 SQLite-only。
2. SQLite Formal E2E PASS。
3. PostgreSQL Formal E2E PASS。
4. MySQL Formal E2E PASS。
5. MongoDB Formal Find PASS。
6. MongoDB Formal Aggregate PASS。
7. Redis Formal Exact-key Read PASS。
8. Redis Formal Bounded Scan PASS。
9. SQL business values 全部参数化。
10. MongoDB/Redis 只接受 compiler 产生的 typed read payload。
11. Native Validator 对 SQL/MongoDB/Redis 都独立 Fail Closed。
12. Provenance(command + parameters) 三类语言都验证。
13. Revision Fence 在所有正式执行路径都有效。
14. Public Evidence 不泄露 business parameters/secrets。
15. Legacy Explicit SQL / legacy adapter execute 兼容路径不破坏。
16. IQ-01 全量行为回归通过。
17. `pytest -q` PASS。
18. `ruff check .` PASS。
19. 真实五库环境能访问则真实验收 PASS；环境凭据/网络不可用则明确 BLOCKED，不伪造 PASS。
20. IQ-03 NOT STARTED。

---

## 19. Codex 最终报告

```text
IQ-02 status: PASS / FAIL / BLOCKED

Baseline:
- ...

Changed architecture:
- native contract:
- compiler dispatch:
- validator:
- executor port:

Database capability:
- SQLite:
- PostgreSQL:
- MySQL:
- MongoDB:
- Redis:

Changed files:
- ...

Security invariants:
- parameterization:
- read-only:
- provenance:
- revision fence:
- public evidence redaction:

Tests:
- targeted:
- full pytest:
- ruff:

Real environment:
- SQLite:
- PostgreSQL:
- MySQL:
- MongoDB:
- Redis:

Acceptance:
- A:
- B:
- C:
- D:
- E:
- F:
- G:
- H:
- I:
- J:

IQ-01 regression:
- ...

Known limitations:
- ...

Git commit:
- ...

Next card:
- IQ-03 NOT STARTED
```
